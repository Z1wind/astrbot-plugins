# -*- coding: utf-8 -*-
"""astrbot_plugin_sheep_fund_rank —— 羊村基金涨跌榜。

功能：
  1. @机器人 说「涨跌榜」→ 发送群内成员理财数据：用户 + 涨跌 + 现资金 + 盈亏。
  2. @机器人 说「导入数据」并附截图 → 用图像识别大模型（deepseek-v4-flash-vision）
     识别截图中的基金持仓数据并保存到本地 SQLite。
  3. 每日定时（默认 20:00）自动查询天天基金当日估值涨跌，更新成员资金，
     更新完成后在群内提示「基金已更新」。

数据源：
  - 基金当日估值涨跌：天天基金估值接口 https://fundgz.1234567.com.cn/js/{code}.js
  - 图像识别：读取 AstrBot 主配置 provider_sources 中的 base_url / key，
    统一使用 deepseek-v4-flash-vision 多模态模型；当前网关没有该模型时退回
    deepseek-v4-flash-vision-exp。

不依赖第三方库（仅标准库 + httpx，AstrBot 自带）。
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import hashlib
import json
import os
import re
import sqlite3
import ssl
import tempfile
import threading
import time
from datetime import datetime, timedelta
import urllib.request, urllib.parse
from pathlib import Path

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Star

PLUGIN_VERSION: str = "1.0.0"

# 自愈：早期版本用「包装 sqlite3.connect」的方式做写库通知，那个包装函数会自我递归
# （connect 里又调 connect），一旦装进进程，本进程里所有 sqlite 连接都会 RecursionError，
# 插件也就再也重载不起来了。这里在导入时检测并把原生 connect 换回去。
try:
    if getattr(sqlite3.connect, "__name__", "") == "_patched_connect":
        sqlite3.connect = sqlite3.dbapi2.connect
        logger.warning("[羊村基金涨跌榜] 检测到被污染的 sqlite3.connect，已恢复原生实现")
except Exception:
    pass

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

UPDATE_HOUR = 20   # 每日基金更新的小时（默认，可被 config.daily_update_time 覆盖）
UPDATE_MIN = 0     # 每日基金更新的分钟（默认，可被 config.daily_update_time 覆盖）
RECALL_PROMPT_SEC = 60   # 即时提示消息发送后多少秒自动撤回
RECALL_NOTICE_SEC = 60  # 每日更新/预约 notice 发送后多少秒自动撤回（QQ 官方 bot 撤回时限仅 2 分钟，必须 <120）
_DEFAULT_CONFIRM_TIME = "12:00"  # 预约确认没给时刻时的默认时间（可被 config 覆盖）

# 2026 年中国股市/基金休市日（沪深北交易所公告；含周末，周末另由 weekday 判断）。
# 周末与这些节假日不自动更新基金净值。跨年需按交易所公告续维护。
_MARKET_HOLIDAYS_2026 = {
    # 元旦 1/1-1/3
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节 2/14-2/23、2/28（2/15-2/23休市，2/14、2/28周末）
    "2026-02-14", "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18",
    "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    "2026-02-28",
    # 清明 4/4-4/6
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 劳动节 5/1-5/5、5/9
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-05-09",
    # 端午 6/19-6/21
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 中秋 9/20、9/25-9/27
    "2026-09-20", "2026-09-25", "2026-09-26", "2026-09-27",
    # 国庆 10/1-10/7、10/10
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    "2026-10-06", "2026-10-07", "2026-10-10",
}

def _game_mode_active() -> bool:
    """游戏模式标记：由 scripts/game_mode.ps1 在 CS2 运行时创建。
    只影响每日自动更新，不影响手动查询/导入/预约确认。"""
    try:
        base = Path(os.environ.get("USERPROFILE") or str(Path.home()))
        return (base / ".astrbot" / "game_mode.on").exists()
    except OSError:
        return False


_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF"
    "\U0001F900-\U0001F9FF\U0001FA70-\U0001FAFF\ufe0f\u200d]")


def _norm_member(name: str) -> str:
    """成员名规范化：去掉昵称中的 emoji/特殊符号，避免同一个人因昵称不同被拆成两个成员。"""
    s = _EMOJI_RE.sub("", name or "").strip()
    return s or (name or "").strip()


def _is_market_open(today: str) -> bool:
    """判断某日期（YYYY-MM-DD）是否为国内基金交易日：周末或法定节假日休市返回 False。"""
    global _HOLIDAY_YEAR_WARNED
    try:
        y, m, d = map(int, today.split("-"))
        if datetime(y, m, d).weekday() >= 5:  # 周六、周日
            return False
    except Exception:
        return True
    if y != 2026 and not _HOLIDAY_YEAR_WARNED:
        # 节假日表只维护到 2026 年；跨年后只按周末判断，会漏掉法定假日
        _HOLIDAY_YEAR_WARNED = True
        logger.warning(f"[羊村基金涨跌榜] {y} 年法定节假日表尚未更新（现只有 2026 年），"
                       f"跨年请补 _MARKET_HOLIDAYS_{y}，否则节假日仍会推送。")
    return today not in _MARKET_HOLIDAYS_2026


_HOLIDAY_YEAR_WARNED = False


def _a_share_preopen(now=None) -> bool:
    """A股是否还没开盘（交易日 09:30 之前，含 09:00-09:25 的集合竞价）。
    这段时间 ETF/个股报价还是「昨收/0」，直接当 0.00% 显示会误导；
    A50 期货 09:00 已开盘，可作为盘前参考。"""
    now = now or time.localtime()
    today = time.strftime("%Y-%m-%d", now)
    if not _is_market_open(today):
        return False
    return now.tm_hour * 60 + now.tm_min < 9 * 60 + 30


def _next_trading_day(day: str) -> str:
    """返回 day（含）之后第一个交易日；day 本身是交易日则原样返回。"""
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
    except Exception:
        return day
    for _ in range(30):
        s = d.strftime("%Y-%m-%d")
        if _is_market_open(s):
            return s
        d = d + timedelta(days=1)
    return day


def _trading_day_offset(day: str, n: int) -> str:
    """从 day 往后数 n 个交易日（n=1 表示下一个交易日）。"""
    cur = day
    for _ in range(max(int(n), 0)):
        nxt = (datetime.strptime(cur, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        cur = _next_trading_day(nxt)
    return cur


def _default_convert_out_date(now: datetime | None = None) -> str:
    """默认转出日：交易日 15:00 前提交=今天，否则顺延到下一个交易日。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    if _is_market_open(today) and now.hour < 15:
        return today
    return _trading_day_offset(today, 1)


def _default_convert_confirm_at(out_date: str, qdii: bool, anchor: str = "12:00") -> str:
    """默认份额确认时间：国内基金 T+1、QDII T+2，取默认时刻。"""
    return f"{_trading_day_offset(out_date, 2 if qdii else 1)} {anchor or '12:00'}"


# 群成员昵称库（QQ昵称 → QQ号），用于页面成员选择与成员概览
GROUP_MEMBERS: dict[str, str] = {}
# 成员昵称 -> QQ 映射由使用者在本地配置/扩展，公开仓库不包含真实群成员数据。

# 天天基金历史净值/涨跌接口：Data_netWorthTrend 数组最后一项的 equityReturn 即最新净值涨跌幅 %
FUND_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"

# 真实头像缓存目录：plugins/astrbot_plugin_sheep_fund_rank/avatars/<成员名>.jpg
AVATAR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avatars")


def _safe_filename(name: str) -> str:
    """把成员名转成安全的 Windows 文件名（与头像下载脚本一致）。"""
    for ch in ['/', ':', '*', '?', '"', '<', '>', '|', '\\']:
        name = name.replace(ch, '_')
    return name

# 数据表建表语句
DDL = """
CREATE TABLE IF NOT EXISTS members_funds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member      TEXT NOT NULL,          -- 成员昵称
    fund_code   TEXT NOT NULL,          -- 基金代码（6 位）
    fund_name   TEXT DEFAULT '',        -- 基金名称
    principal   REAL NOT NULL,          -- 投入本金（已确认份额）
    current     REAL NOT NULL,          -- 当前资金（已确认份额现值）
    last_change REAL DEFAULT 0,         -- 最近一次更新时的当日涨跌幅 %
    last_update TEXT DEFAULT '',        -- 最近一次更新日期 YYYY-MM-DD
    confirmed   INTEGER DEFAULT 1,      -- 1=已确认参与更新, 0=待确认(保存但不更新)
    pending_amount REAL DEFAULT 0,      -- 待确认的买入挂账金额（份额未确认，确认后并入现值）
    pending_principal REAL DEFAULT 0,  -- 挂账对应的成本本金（基金转换结转浮亏用；普通买入=0，确认时按pending_amount入本金）
    confirm_date TEXT DEFAULT '',       -- 确认份额生效日（净值基准日），该日之后才参与每日更新
    shares      REAL DEFAULT 0          -- 持有份额：>0 时现值=份额×最新净值（不再按涨跌幅复利累乘）
    , realized_pnl REAL DEFAULT 0       -- 累计已实现盈亏（卖出结算，正=赚，负=亏）
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_member_fund
    ON members_funds(member, fund_code);
CREATE TABLE IF NOT EXISTS confirm_schedules (
    member      TEXT NOT NULL,           -- 成员昵称
    fund_code   TEXT NOT NULL,           -- 基金代码
    confirm_at  TEXT NOT NULL,           -- 该条记录的预约确认份额时间 YYYY-MM-DD HH:MM
    PRIMARY KEY (member, fund_code)
);
CREATE TABLE IF NOT EXISTS trade_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member      TEXT NOT NULL,           -- 成员昵称
    fund_code   TEXT NOT NULL,           -- 基金代码
    fund_name   TEXT DEFAULT '',         -- 基金名称
    op_type     TEXT NOT NULL,           -- 操作类型：买入 / 卖出
    amount      REAL NOT NULL,           -- 金额（元）
    ts          TEXT NOT NULL            -- 时间 YYYY-MM-DD HH:MM:SS
);
CREATE TABLE IF NOT EXISTS meta (
    k           TEXT PRIMARY KEY,        -- 键（如 notified_day）
    v           TEXT DEFAULT ''          -- 值
);
CREATE TABLE IF NOT EXISTS conversions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member      TEXT NOT NULL,           -- 成员昵称
    src_code    TEXT NOT NULL,           -- 转出基金代码
    src_name    TEXT DEFAULT '',
    tgt_code    TEXT NOT NULL,           -- 转入基金代码
    tgt_name    TEXT DEFAULT '',
    out_date    TEXT NOT NULL,           -- 转出日（赎回按此日净值结算）YYYY-MM-DD
    confirm_at  TEXT DEFAULT '',         -- 转入预约确认时间 YYYY-MM-DD HH:MM
    amount      REAL DEFAULT 0,          -- 结算金额（转出日晚回填）
    part_amount REAL DEFAULT 0,          -- 计划转出金额（0=整只转出）
    part_ratio  REAL DEFAULT 0,          -- 计划转出比例（0~1；老记录用，新记录走份额）
    out_shares  REAL DEFAULT 0,          -- 计划转出份额（按份额转换：金额=份额×转出日净值）
    in_nav      REAL DEFAULT 0,          -- 转入确认净值（T+1，结算/确认时回填）
    in_shares   REAL DEFAULT 0,          -- 转入份额（= 转入金额 ÷ 转入确认净值）
    status      TEXT DEFAULT 'active',  -- active / settled
    created     TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pending_lots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    member        TEXT NOT NULL,         -- 成员昵称
    fund_code     TEXT NOT NULL,         -- 转入基金代码
    fund_name     TEXT DEFAULT '',
    amount        REAL DEFAULT 0,        -- 本批挂账现值（元）
    principal     REAL DEFAULT 0,        -- 本批成本本金（浮亏结转用）
    confirm_at    TEXT DEFAULT '',       -- 本批份额确认时间 YYYY-MM-DD HH:MM
    conversion_id INTEGER DEFAULT 0,     -- 来源在途转换 id（0=非转换来源）
    notified      INTEGER DEFAULT 0,     -- 1=已就该批次发过到点提示
    shares        REAL DEFAULT 0,        -- 本批转入份额（确认时并入，0=按金额反推）
    nav           REAL DEFAULT 0,        -- 本批转入净值（算份额用的那个净值）
    status        TEXT DEFAULT 'pending',-- pending / confirmed
    created       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pending_lots
    ON pending_lots(member, fund_code, status);
CREATE TABLE IF NOT EXISTS pending_sells (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    member        TEXT NOT NULL,         -- 成员昵称
    fund_code     TEXT NOT NULL,         -- 卖出基金代码
    fund_name     TEXT DEFAULT '',
    shares        REAL DEFAULT 0,        -- 冻结卖出的份额（0=只记了挂单还没冻结）
    expected      REAL DEFAULT 0,        -- 预计到手金额（0=未知）
    principal_part REAL DEFAULT 0,       -- 卖出部分对应的成本本金（结算时结转已实现）
    actual        REAL DEFAULT 0,        -- 实际到手金额（结算后回填）
    confirmed     INTEGER DEFAULT 0,     -- 1=已结算
    confirm_at    TEXT DEFAULT '',       -- 预计确认/到账时间 YYYY-MM-DD HH:MM
    notified      INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'pending',-- pending / done / cancelled
    created       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pending_sells
    ON pending_sells(member, fund_code, status);
"""

# 卖出挂单表：老库可能还没建，用到时按需补建（幂等）
PENDING_SELLS_DDL = """
CREATE TABLE IF NOT EXISTS pending_sells (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    member        TEXT NOT NULL,
    fund_code     TEXT NOT NULL,
    fund_name     TEXT DEFAULT '',
    shares        REAL DEFAULT 0,
    expected      REAL DEFAULT 0,
    principal_part REAL DEFAULT 0,
    actual        REAL DEFAULT 0,
    confirmed     INTEGER DEFAULT 0,
    confirm_at    TEXT DEFAULT '',
    notified      INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'pending',
    created       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pending_sells
    ON pending_sells(member, fund_code, status);
"""

OCR_PROMPT = (
    "这是一张基金/理财相关截图。请先用大模型理解图片内容，判断截图类型（输出到 page_type 字段）："
    "'持仓页'（支付宝/天天基金等持仓列表，含基金名称和持有金额/份额）、"
    "'交易明细'（买卖/申购/赎回记录，含买入卖出金额）、或 '其他'。"
    "然后精确提取每一只基金的信息：基金名称、基金代码（6位数字，没有则空）、以及作为【本金/持有金额】的数字 amount（单位：元）。"
    "⚠️ 字段口径务必准确：只把『持有金额/持有市值/买入金额/申购金额』记入 amount；"
    "绝对不要把『持有份额』『份额』『持有收益』『昨日收益』『累计收益』当作 amount。"
    "份额和收益请分别输出到 shares / income 字段（没有则留空）。"
    "输出一个 JSON 对象，格式："
    "{\"page_type\":\"持仓页\",\"date\":\"数据日期或估值时间如 2026-10-21，没有则留空\","
    "\"records\":[{\"fund_name\":\"基金名称\",\"fund_code\":\"基金代码（6位数字，没有则空）\","
    "\"amount\":\"金额数字\",\"shares\":\"份额数字或空\",\"income\":\"收益数字或空\"}]}。"
    "只输出 JSON，不要任何额外说明文字。字段缺失用空字符串。"
    "金额务必理解准确，不要加逗号和货币符号。不要输出成员名字。"
    "若截图根本不是基金数据页（如无关图片），records 输出空数组 []。"
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _data_root() -> Path:
    up = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(up) / ".astrbot" / "data"


def _db_path() -> Path:
    p = _data_root() / "plugin_data" / "sheep_fund_rank" / "fund.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _plugin_cfg_file() -> Path:
    return _data_root() / "config" / "astrbot_plugin_sheep_fund_rank_config.json"


# 进程内缓存：重仓股几乎按季报变、行情秒级刷新、群 umo 基本不变，缓存能省掉大量 HTTP/DB 往返
_HOLDINGS_CACHE: dict[str, tuple[float, str, list]] = {}   # code -> (ts, fname, holds)
_ESTIMATE_CACHE: dict[str, tuple[float, tuple]] = {}        # code -> (ts, (est, cover, kind, fname))
_GROUP_UMOS_CACHE: dict[str, tuple[float, list]] = {}       # "_" -> (ts, umos)
HOLDINGS_TTL = 6 * 3600     # 重仓股：6 小时
ESTIMATE_TTL = 60           # 盘中估算：1 分钟（同分钟内的多次查询复用）
GROUP_UMOS_TTL = 60         # 群会话列表：1 分钟

# 指数/ETF 联接基金 → 对应场内 ETF（官方实时估值已下线，用场内行情近似）
_ETF_MAP = {
    "010524": "159994",  # 银华中证5G通信主题ETF联接 → 通信ETF银华
    "020640": "560780",  # 广发中证半导体材料设备ETF联接 → 芯片设备ETF
    "009504": "518680",  # 富国上海金ETF联接 → 金ETF富国
    "016581": "159831",  # 嘉实上海金ETF联接 → 金嘉实
    "021458": "563020",  # 易方达中证红利低波动ETF联接 → 红利低波ETF
    "008164": "515450",  # 南方标普红利低波50ETF联接C → 红利低波50ETF南方
    "016452": "513100",  # 南方纳斯达克100(QDII) → 纳指ETF(国泰)近似
}


def _plugin_cfg_value(key: str, default=None):
    """读插件配置。优先直接读插件配置文件。
    但 metadata 新增的键在 AstrBot 完整重启前不会进入 schema（会被丢弃），
    这时直接读插件配置文件，改完配置不用重启就生效。"""
    try:
        raw = _plugin_cfg_file().read_text(encoding="utf-8-sig").lstrip("\ufeff")
        data = json.loads(raw)
        if key in data and data[key] is not None:
            return data[key]
    except Exception:
        pass
    return default


# ---------------------------------------------------------------------------
# 桌面组件联动：fund.db 每次「写库提交」，顺手通知桌面 exe「羊村基金组件」重读数据。
# 完全事件驱动：不发定时器、不轮询、不读秒。
#   · 写库 → sqlite3 连接 commit() 钩子 → 唤醒一个空闲的后台线程（Event.wait 阻塞，0 CPU）
#   · 后台线程等 1.5 秒把同一轮的多次 commit 合并成一次，再 POST /api/notify
#   · 没装/没开 exe（读不到 widget_port.txt）就直接返回，不做任何网络请求
# ---------------------------------------------------------------------------
_WIDGET_EVT = threading.Event()
_WIDGET_LAST = 0.0
_WIDGET_MIN_GAP = 3.0     # 两次通知最小间隔（秒）
_WIDGET_DEBOUNCE = 1.5    # 合并同一批写库的等待时间
_WIDGET_LOCK = threading.Lock()
_WIDGET_THREAD = None


def _widget_port() -> int:
    """读桌面组件写下的监听端口；没装/没开就返回 0。"""
    try:
        pf = _db_path().parent / "widget_port.txt"
        if not pf.exists():
            return 0
        txt = (pf.read_text(encoding="utf-8", errors="ignore") or "").strip()
        port = int(re.sub(r"\D", "", txt) or 0)
        return port if 1024 < port < 65536 else 0
    except Exception:
        return 0


def _widget_post(reason: str = "db") -> bool:
    """真正发通知（只在后台线程里调用，短超时、失败静默）。"""
    port = _widget_port()
    if not port:
        return False
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/notify",
            data=json.dumps({"event": "db_update", "reason": reason}).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            ok = r.status < 300
        if ok:
            logger.info(f"[羊村基金涨跌榜] 已通知桌面组件刷新（{reason} · port {port}）")
        return ok
    except Exception as e:
        # 连不上 = exe 没开（或退出后端口文件残留）：删掉残留文件，等它下次启动重写
        try:
            pf = _db_path().parent / "widget_port.txt"
            if pf.exists() and (pf.read_text(encoding="utf-8", errors="ignore").strip() == str(port)):
                pf.unlink()
                logger.info("[羊村基金涨跌榜] 桌面组件未运行，已清理残留端口文件")
                return False
        except Exception:
            pass
        logger.info(f"[羊村基金涨跌榜] 桌面组件未响应（可能没开），本次跳过: {e}")
        return False


def _widget_worker(reason_box: list) -> None:
    """后台线程：Event.wait 阻塞时完全不占 CPU，被唤醒才干活。
    唤醒 → 等 1.5 秒合并同一批写库 → 若与上次通知间隔不足 3 秒，就等够再发（不丢这次）。"""
    global _WIDGET_LAST
    while True:
        _WIDGET_EVT.wait()
        _WIDGET_EVT.clear()
        time.sleep(_WIDGET_DEBOUNCE)      # 合并同一批写库
        gap = time.time() - _WIDGET_LAST
        if gap < _WIDGET_MIN_GAP:
            time.sleep(_WIDGET_MIN_GAP - gap)
        _WIDGET_EVT.clear()           # 这一批提交都由本次通知覆盖，别再多发一轮
        _WIDGET_LAST = time.time()
        reason = reason_box[0] if reason_box else "db"
        try:
            _widget_post(reason)
        except Exception:
            pass


def _notify_widget(reason: str = "db") -> None:
    """写库提交后调用：只置一个事件标志，立刻返回（不做网络请求、不阻塞）。"""
    global _WIDGET_THREAD
    if _widget_port() <= 0:      # 没开 exe 连文件都不用看第二次，直接走人
        return
    if _WIDGET_THREAD is None or not _WIDGET_THREAD.is_alive():
        with _WIDGET_LOCK:
            if _WIDGET_THREAD is None or not _WIDGET_THREAD.is_alive():
                _WIDGET_THREAD = threading.Thread(
                    target=_widget_worker, args=([reason],), daemon=True,
                    name="sheep-widget-notify")
                _WIDGET_THREAD.start()
    _WIDGET_EVT.set()


class _WatchConn(sqlite3.Connection):
    """只给 fund.db 用的连接：提交完成后通知桌面组件（不碰全局 sqlite3.connect）。"""

    def commit(self):
        super().commit()
        try:
            _notify_widget("db")
        except Exception:
            pass


def _db_connect(*a, **k) -> sqlite3.Connection:
    k.setdefault("factory", _WatchConn)
    return sqlite3.connect(*a, **k)


def _load_cmd_config() -> dict | None:
    p = _data_root() / "cmd_config.json"
    if not p.exists():
        return None
    try:
        # 这个文件被反复保存过，可能带 1~2 个 UTF-8 BOM；utf-8-sig 只去一个，
        # 剩下一个会让 json.load 直接失败，进而让所有模型探测都“没有可用模型”。
        raw = p.read_text(encoding="utf-8-sig").lstrip("\ufeff")
        return json.loads(raw)
    except Exception:
        return None


MODEL_PREFERRED = "deepseek-v4-flash-vision"      # 统一使用的模型
MODEL_FALLBACK = "deepseek-v4-flash-vision-exp"   # 仅当前网关没有首选模型时退回
_MODEL_CACHE: dict[str, dict] = {}                # role -> {endpoint, checked}
_MODEL_CACHE_TTL = 1800
_MODEL_LAST_CALL = 0.0
_MODEL_CALL_LOCK: asyncio.Lock | None = None


def _provider_sources() -> dict[str, dict]:
    cfg = _load_cmd_config() or {}
    return {p.get("id"): p for p in cfg.get("provider_sources") or [] if isinstance(p, dict)}


def _endpoint_for_source(source_id: str, model: str) -> tuple[str, str, str] | None:
    src = _provider_sources().get(source_id)
    if not src:
        return None
    base = src.get("base_url") or src.get("api_base") or ""
    key = src.get("api_key") or src.get("key") or src.get("keys")
    if isinstance(key, list):
        key = key[0] if key else None
    if not base or not key:
        return None
    return base.rstrip("/"), str(key), model


def _model_candidates() -> list[tuple[str, str, str, str]]:
    """按优先级列出候选 (来源, base, key, model)。

    首选 deepseek-v4-flash-vision：不同网关挂在不同 provider_source 下，
    例如 openai 有真模型、openai_1 只有 -exp。这里逐个探测，优先用首选。
    """
    cfg = _load_cmd_config() or {}
    out: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    for model in (MODEL_PREFERRED, MODEL_FALLBACK):
        for prov in cfg.get("provider") or []:
            if not isinstance(prov, dict) or str(prov.get("model") or "") != model:
                continue
            sid = str(prov.get("provider_source_id") or "")
            ep = _endpoint_for_source(sid, model)
            if ep and (sid, model) not in seen:
                seen.add((sid, model))
                out.append((sid, *ep))
        # 网关可能直接支持该模型，但 AstrBot 的 provider 列表里尚未登记
        for sid in ("openai", "openai_1"):
            ep = _endpoint_for_source(sid, model)
            if ep and (sid, model) not in seen:
                seen.add((sid, model))
                out.append((sid, *ep))
    return out


def _probe_endpoint(base: str, key: str, model: str, timeout: int = 20) -> bool:
    global _MODEL_LAST_CALL
    payload = json.dumps({"model": model, "max_tokens": 8,
                          "messages": [{"role": "user", "content": "ping"}]}).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        return bool((d.get("choices") or []) or d.get("model"))
    except Exception as e:
        logger.info("[羊村基金涨跌榜] 模型探测失败 %s: %s", model, e)
        return False
    finally:
        _MODEL_LAST_CALL = time.time()


def _fallback_endpoint(base: str, key: str, model: str) -> tuple[str, str, str] | None:
    for _sid, b, k, m in _model_candidates():
        if m == MODEL_FALLBACK and (b, k, m) != (base, key, model):
            return b, k, m
    return None


def _remember_fallback(current: tuple[str, str, str], fallback: tuple[str, str, str]) -> None:
    for role in ("vision", "text"):
        state = _MODEL_CACHE.get(role)
        if not state or state.get("endpoint") == current:
            _MODEL_CACHE[role] = {"endpoint": fallback, "checked": time.time()}


async def _post_chat(base: str, key: str, payload: dict, timeout: int = 40) -> dict:
    """统一调用模型接口：串行限速；首选限流/故障时自动切到 -exp。"""
    global _MODEL_LAST_CALL, _MODEL_CALL_LOCK
    if _MODEL_CALL_LOCK is None:
        _MODEL_CALL_LOCK = asyncio.Lock()
    async with _MODEL_CALL_LOCK:
        model = str(payload.get("model") or "")
        current = (base, key, model)
        fallback = _fallback_endpoint(base, key, model)
        candidates = [current] + ([fallback] if fallback else [])
        last_exc: Exception | None = None

        for idx, (b, k, m) in enumerate(candidates):
            p = dict(payload)
            p["model"] = m
            for attempt in range(2):
                wait = 1.2 - (time.time() - _MODEL_LAST_CALL)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
                        r = await client.post(
                            f"{b}/chat/completions",
                            headers={"Authorization": f"Bearer {k}",
                                     "Content-Type": "application/json"},
                            json=p,
                        )
                        if r.status_code == 429:
                            last_exc = RuntimeError("429 Too Many Requests")
                            if attempt == 0:
                                await asyncio.sleep(1.0)
                                continue
                            break
                        r.raise_for_status()
                        return r.json()
                except Exception as e:
                    last_exc = e
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                        continue
                    break
                finally:
                    _MODEL_LAST_CALL = time.time()

            if idx == 0 and fallback:
                logger.info("[羊村基金涨跌榜] 模型 %s 暂时不可用（%s），切换到 %s",
                            model, last_exc, fallback[2])
                _remember_fallback(current, fallback)

        raise last_exc or RuntimeError("模型接口请求失败")


def _model_endpoint(vision: bool) -> tuple[str, str, str] | None:
    """统一模型选择：首选可用就用首选，否则用同系列回退版本。"""
    role = "vision" if vision else "text"
    now = time.time()
    cached = _MODEL_CACHE.get(role)
    if cached and now - cached.get("checked", 0) < _MODEL_CACHE_TTL:
        return cached.get("endpoint")

    for sid, base, key, model in _model_candidates():
        if _probe_endpoint(base, key, model):
            endpoint = (base, key, model)
            _MODEL_CACHE[role] = {"endpoint": endpoint, "checked": now}
            logger.info("[羊村基金涨跌榜] 使用模型 %s（来源 %s）", model, sid)
            return endpoint

    _MODEL_CACHE[role] = {"endpoint": None, "checked": now}
    logger.warning("[羊村基金涨跌榜] 没有可用模型（%s / %s），请检查 provider 配置",
                   MODEL_PREFERRED, MODEL_FALLBACK)
    return None


def _openai1_vision() -> tuple[str, str, str] | None:
    """返回 (base_url, api_key, model)；模型统一为 deepseek-v4-flash-vision。"""
    return _model_endpoint(vision=True)


def _openai1_llm() -> tuple[str, str, str] | None:
    """返回 (base_url, api_key, model)；文本调用也统一使用视觉模型。"""
    return _model_endpoint(vision=False)


async def _llm_parse_confirm(text: str) -> dict:
    """用当前视觉模型理解用户的导入确认回复，提取日期和各基金金额。"""
    o1 = _openai1_llm()
    if not o1:
        return {"date": "", "funds": []}
    base, key, model = o1
    prompt = (
        "用户正在确认一批基金导入数据。请用大模型理解下面回复，提取："
        "1) 数据日期/时间（如有）；2) 各只基金的金额或份额。"
        "输出严格 JSON，格式："
        "{\"date\":\"日期，没有则空\",\"funds\":[{\"fund_name\":\"基金名称或代码\","
        "\"amount\":\"金额数字\"}]}。只输出 JSON，不要任何额外说明。"
        "基金名保持原文。用户回复：\n" + text
    )
    payload = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        data = await _post_chat(base, key, payload, timeout=40)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = re.sub(r"```(?:json)?", "", content).strip().strip("`")
        obj = json.loads(content)
        funds = obj.get("funds") if isinstance(obj, dict) else obj
        out = []
        if isinstance(funds, list):
            for f in funds:
                if not isinstance(f, dict):
                    continue
                fname = str(f.get("fund_name", "") or "").strip()
                try:
                    amount = float(str(f.get("amount", "") or "").replace(",", "").strip())
                except (TypeError, ValueError):
                    amount = 0.0
                if fname and amount > 0:
                    out.append({"fund_name": fname, "amount": amount})
        return {"date": str(obj.get("date", "") or "").strip() if isinstance(obj, dict) else "",
                "funds": out}
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 确认解析失败: {e}")
        return {"date": "", "funds": []}


async def _llm_parse_confirm_time(text: str) -> str | None:
    """用当前视觉模型把用户随意说的确认时间解析为绝对时间 YYYY-MM-DD HH:MM。"""
    o1 = _openai1_llm()
    if not o1:
        return None
    base, key, model = o1
    today = time.strftime("%Y-%m-%d %A")
    prompt = (
        f"今天是{today}。用户在用非常随意、口语化的话说一个『确认基金份额/到账/转正』的时间。"
        "可能的表达有：『周四确认』『周四才确认』『下周三到账』『份额月底才能看到』"
        "『明天晚上确认份额』『今晚确认』『确认份额后就更新』『周五份额到账』『周一』等。"
        f"请把它解析成绝对时间，输出严格 JSON {{\"datetime\":\"YYYY-MM-DD HH:MM\"}}。"
        f"规则：只给了星期/日期没给具体时刻时，默认当天 {_DEFAULT_CONFIRM_TIME}；"
        "『周X』换算成最近一个满足条件的未来日期；『下月/月底』按日历理解；"
        "若解析不出一个有效的未来时间，输出 {\"datetime\":\"\"}。只输出 JSON，不要任何说明。用户说：" + text
    )
    payload = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        data = await _post_chat(base, key, payload, timeout=40)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = re.sub(r"```(?:json)?", "", content).strip().strip("`")
        obj = json.loads(content)
        dt = str(obj.get("datetime", "") or "").strip()
        return datetime.strptime(dt, "%Y-%m-%d %H:%M") if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", dt) else None
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 确认时间解析失败: {e}")
        return None


def _num_or_none(v, allow_negative: bool = False) -> float | None:
    """把模型给的数字/字符串安全转成金额；非法或离谱返回 None。"""
    try:
        if v is None or v == "":
            return None
        f = float(v)
        if (f < 0 and not allow_negative) or abs(f) >= 1e8:
            return None
        return round(f, 2)
    except (TypeError, ValueError):
        return None


def _match_fund_code(text: str, funds: list[dict]) -> str:
    """在成员自己的记录里按代码/名称模糊匹配基金代码（对账用）。"""
    t = (text or "").strip()
    if not t:
        return ""
    for r in funds:
        if r["fund_code"] == t:
            return r["fund_code"]
    for r in funds:
        fn = r["fund_name"] or ""
        if fn and (t in fn or fn in t):
            return r["fund_code"]
    best, best_score = "", 0.0
    for r in funds:
        fn = r["fund_name"] or ""
        if not fn:
            continue
        score = len(set(t) & set(fn)) / max(len(set(fn)), 1)
        if score > best_score:
            best, best_score = r["fund_code"], score
    return best if best_score >= 0.6 else ""


async def _llm_parse_recon_ops(text: str, funds: list[dict]) -> list[dict]:
    """把「对账 永赢医药 本金 500」这类口语解析成结构化改动。解析不出返回 []。"""
    o1 = _openai1_llm()
    if not o1:
        return []
    base, key, model = o1
    listing = "\n".join(
        f"- {r['fund_name']}({r['fund_code']}) 本金{r['principal']:.2f} 现值{r['current']:.2f}"
        for r in funds[:40]) or "（暂无记录）"
    prompt = (
        "用户在维护一个基金台账，他要用口语修改自己名下的基金数据。这是他名下的基金：\n"
        f"{listing}\n\n"
        "请解析成严格 JSON：{\"ops\":[{\"code\":\"基金代码\",\"name\":\"基金名\","
        "\"principal\":数字或null,\"current\":数字或null,\"action\":\"set\"}]}\n"
        "规则：\n"
        "1) 「本金/成本」→ 填 principal；「现值/市值/金额/现在多少钱」→ 填 current；"
        "只给一个数字没说是哪一栏 → 填 current。\n"
        "2) 「删掉/删除/不要这只」→ action=delete。\n"
        "3) 基金名能对上清单就填对应的 code；对不上 code 留空、name 用用户原话。\n"
        "4) 没提到具体数字就不要生成 op。只输出 JSON，不要解释。用户说：" + text
    )
    payload = {"model": model, "temperature": 0,
               "messages": [{"role": "user", "content": prompt}]}
    try:
        data = await _post_chat(base, key, payload, timeout=45)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = re.sub(r"```(?:json)?", "", content).strip().strip("`")
        obj = json.loads(content)
        ops = []
        for o in obj.get("ops") or []:
            if not isinstance(o, dict):
                continue
            code = str(o.get("code") or "").strip()
            name = str(o.get("name") or "").strip()
            if not code and name:
                code = _match_fund_code(name, funds)
            if not code:
                continue
            kind = "delete" if str(o.get("action") or "") == "delete" else "set"
            ops.append({"kind": kind, "code": code, "name": name,
                        "principal": _num_or_none(o.get("principal")),
                        "current": _num_or_none(o.get("current"))})
        return [o for o in ops
                if o["kind"] == "delete" or o["principal"] is not None or o["current"] is not None]
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 对账指令解析失败: {e}")
        return []


async def _llm_parse_confirm_times(text: str, count: int) -> dict:
    """用当前视觉模型把用户为『预约确认』选中的多条记录各说的时间解析出来。
    支持：单个时间（应用到全部选中条）→ {0: datetime}；
    或逐条如『1周四 2周五15:00』→ {1: datetime, 2: datetime}。
    解析失败返回 {}。"""
    o1 = _openai1_llm()
    if not o1:
        return {}
    base, key, model = o1
    today = time.strftime("%Y-%m-%d %A")
    prompt = (
        f"今天是{today}。用户在『羊村基金涨跌榜』预约确认时为选中条目设定确认份额的时间，说得随意口语化，"
        f"共选中 {count} 条。可能给出："
        "a) 一个整体时间（如『周四15:00』『明天10点』『月底确认』）应用到全部条；"
        f"b) 逐条时间（如『1周四 2周五15:00』『第1条今晚、第2条明天』），编号 1..{count}。"
        f"请解析为绝对时间 YYYY-MM-DD HH:MM，只给了星期/日期没给时刻默认当天 {_DEFAULT_CONFIRM_TIME}；"
        "『周X』换算成最近未来日期；『今天/今晚』取今天 15:00。"
        f"输出严格 JSON：整体时间 {{\"mode\":\"all\",\"datetime\":\"YYYY-MM-DD HH:MM\"}}；"
        f"逐条 {{\"mode\":\"each\",\"times\":{{\"1\":\"YYYY-MM-DD HH:MM\",...}}}}。"
        "无法解析则 mode 为 empty。只输出 JSON，不要说明。用户说：" + text
    )
    payload = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        data = await _post_chat(base, key, payload, timeout=40)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = re.sub(r"```(?:json)?", "", content).strip().strip("`")
        obj = json.loads(content)
        mode = str(obj.get("mode", "empty")).strip()
        out: dict = {}
        if mode == "all":
            dt = str(obj.get("datetime", "") or "").strip()
            if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", dt):
                out[0] = datetime.strptime(dt, "%Y-%m-%d %H:%M")
        elif mode == "each":
            for k, v in (obj.get("times") or {}).items():
                vs = str(v or "").strip()
                if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", vs):
                    try:
                        out[int(k)] = datetime.strptime(vs, "%Y-%m-%d %H:%M")
                    except (ValueError, TypeError):
                        continue
        return out
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 逐条确认时间解析失败: {e}")
        return {}


async def _llm_parse_schedule_input(text: str, total: int) -> dict:
    """预约确认 choose 阶段：一次性理解『编号 + 时间』。
    支持：只给编号『1 2 3』；编号+整体时间『1 2 3 周四15:00』（这些编号共用该时间）；
    或逐条『1 2 3 周四15:00 4 周五 5 下周一4:00』。
    返回 {"selected": [...], "times": {编号: datetime}}，times 只含明确给了时间的编号。"""
    o1 = _openai1_llm()
    if not o1:
        return {"selected": [], "times": {}}
    base, key, model = o1
    today = time.strftime("%Y-%m-%d %A")
    prompt = (
        f"今天是{today}。用户在『羊村基金涨跌榜』预约确认时，为条目选择编号并设定确认份额的时间，"
        f"共可选 {total} 条，说得随意口语化。可能：a) 只给编号『1 2 3』；"
        f"b) 编号+一个整体时间『1 2 3 周四15:00』（这些编号共用该时间）；"
        f"c) 逐条『1 2 3 周四15:00 4 周五 5 下周一4:00』（各编号各自时间）。"
        f"请解析：selected 是选中的编号列表（数字，去重，必须在 1..{total}）；"
        f"times 只包含明确给了时间的编号，值解析为绝对时间 YYYY-MM-DD HH:MM。"
        f"时间规则：只给了星期/日期没给时刻默认当天 {_DEFAULT_CONFIRM_TIME}；『周X』换算成最近一个未来日期；"
        f"『今天/今晚』取今天 15:00。输出严格 JSON "
        f"{{\"selected\":[1,2,3],\"times\":{{\"1\":\"YYYY-MM-DD HH:MM\"}}}}。只输出 JSON，不要任何说明。用户说：" + text
    )
    payload = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        data = await _post_chat(base, key, payload, timeout=40)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = re.sub(r"```(?:json)?", "", content).strip().strip("`")
        obj = json.loads(content)
        selected = []
        for k in (obj.get("selected") or []):
            try:
                n = int(k)
                if 1 <= n <= total and n not in selected:
                    selected.append(n)
            except (ValueError, TypeError):
                continue
        times: dict = {}
        for k, v in (obj.get("times") or {}).items():
            vs = str(v or "").strip()
            if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", vs):
                try:
                    times[int(k)] = datetime.strptime(vs, "%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    continue
        return {"selected": selected, "times": times}
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 预约输入解析失败: {e}")
        return {"selected": [], "times": {}}


async def _llm_parse_confirm_v2(text: str) -> dict:
    """用当前视觉模型一次理解用户的确认回复：同时提取预约/确认时间、各基金金额、数据日期。
    返回 {"confirm_time": datetime|None, "date": str, "funds": [{"fund_name","amount"}]}。"""
    o1 = _openai1_llm()
    if not o1:
        return {"confirm_time": None, "date": "", "funds": []}
    base, key, model = o1
    today = time.strftime("%Y-%m-%d %A")
    prompt = (
        f"今天是{today}。用户在回复『羊村基金涨跌榜』插件的导入确认或预约确认，说得很随意口语化。"
        "请用大模型理解，提取三部分，只输出严格 JSON："
        "1) confirm_time：用户提到的『确认份额/到账/转正/预约』的时间。可能说『周四确认』『下周三到账』"
        "『份额月底才能看到』『明天晚上确认』『确认份额后就更新』等。"
        "解析成绝对时间字符串 YYYY-MM-DD HH:MM：只给了星期/日期没给时刻就默认当天 "
        f"{_DEFAULT_CONFIRM_TIME}；"
        "『周X』换算成最近一个未来日期；『今天/今晚』取今天 15:00。"
        "若这句话没提任何预约/确认时间，confirm_time 为 null。"
        "2) funds：用户提到的各基金金额/份额确认，格式 [{\"fund_name\":\"基金名或代码\",\"amount\":\"数字\"}]，没有则空数组 []。"
        "3) date：若提到具体数据日期，填 YYYY-MM-DD，否则空字符串。"
        "输出格式：{\"confirm_time\":null 或 \"YYYY-MM-DD HH:MM\",\"date\":\"\",\"funds\":[]}。"
        "只输出 JSON，不要任何说明文字。用户回复：" + text
    )
    payload = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    out = {"confirm_time": None, "date": "", "funds": []}
    try:
        data = await _post_chat(base, key, payload, timeout=40)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = re.sub(r"```(?:json)?", "", content).strip().strip("`")
        obj = json.loads(content)
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 确认合并解析失败: {e}")
        return out
    if not isinstance(obj, dict):
        return out
    dt = str(obj.get("confirm_time", "") or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", dt):
        try:
            out["confirm_time"] = datetime.strptime(dt, "%Y-%m-%d %H:%M")
        except ValueError:
            out["confirm_time"] = None
    out["date"] = str(obj.get("date", "") or "").strip()
    funds = []
    for f in obj.get("funds") or []:
        if not isinstance(f, dict):
            continue
        fname = str(f.get("fund_name", "") or "").strip()
        try:
            amount = float(str(f.get("amount", "") or "").replace(",", "").strip())
        except (TypeError, ValueError):
            amount = 0.0
        if fname and amount > 0:
            funds.append({"fund_name": fname, "amount": amount})
    out["funds"] = funds
    return out


async def _llm_parse_buy_input(text: str, total: int) -> dict:
    """买入基金输入解析：理解『编号 金额』『基金名 金额』，可附确认时间。
    返回 {"index": int|null, "name": str, "amount": float, "confirm_time": "YYYY-MM-DD HH:MM"|""}。
    口语随意：『1 3000』『2 2000 周四确认』『招商白酒 5000 明天15点』『买 5000 中欧红利』。"""
    o1 = _openai1_llm()
    if not o1:
        return {"index": None, "name": "", "amount": 0.0, "confirm_time": ""}
    base, key, model = o1
    today = time.strftime("%Y-%m-%d %A")
    prompt = (
        f"今天是{today}。用户在『羊村基金涨跌榜』里买入一只基金：可回复『编号 金额』（编号指已有持仓条目，"
        f"共 {total} 条），或『基金名 金额』（可为新基金名），口语随意，可附带确认份额时间。"
        f"请解析：index 是选中条目编号(1..{total})，若没给编号则为 null；"
        f"name 是基金名/代码（若给了名称）；amount 是买入金额数字；"
        f"confirm_time 是确认份额时间（若提到），格式 YYYY-MM-DD HH:MM，未提则为空字符串。"
        f"时间规则：只给星期/日期没给时刻默认当天 {_DEFAULT_CONFIRM_TIME}；『周X』换算最近一个未来日期。"
        f"输出严格 JSON {{\"index\":1或null,\"name\":\"\",\"amount\":3000,\"confirm_time\":\"YYYY-MM-DD HH:MM\"}}。"
        f"只输出 JSON。用户说：" + text
    )
    payload = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    out = {"index": None, "name": "", "amount": 0.0, "confirm_time": ""}
    try:
        data = await _post_chat(base, key, payload, timeout=40)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = re.sub(r"```(?:json)?", "", content).strip().strip("`")
        obj = json.loads(content)
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 买入解析失败: {e}")
        return out
    if not isinstance(obj, dict):
        return out
    try:
        ix = obj.get("index")
        if ix is not None:
            out["index"] = int(ix)
    except (TypeError, ValueError):
        out["index"] = None
    out["name"] = str(obj.get("name", "") or "").strip()
    try:
        amt = float(str(obj.get("amount", "") or "").replace(",", "").strip())
        out["amount"] = amt
    except (TypeError, ValueError):
        out["amount"] = 0.0
    ct = str(obj.get("confirm_time", "") or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", ct):
        try:
            datetime.strptime(ct, "%Y-%m-%d %H:%M")
            out["confirm_time"] = ct
        except ValueError:
            out["confirm_time"] = ""
    return out


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _make_jwt(secret: str, username: str) -> str:
    h = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = _b64url(
        json.dumps({"username": username, "exp": int(time.time()) + 604800}).encode()
    )
    s = _b64url(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{s}"


def _last_active_group_umo() -> str:
    """取最近有消息的群会话 umo（`平台:GroupMessage:<gid>`）。
    广播目标不再写死某个群：旧群可能已注销，消息会直接发失败。"""
    p = _data_root() / "data_v4.db"
    if not p.exists():
        return ""
    try:
        uri = "file:" + str(p).replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        cur = con.cursor()
        row = cur.execute(
            "select user_id from platform_message_history "
            "where user_id like '%:GroupMessage:%' "
            "order by updated_at desc limit 1"
        ).fetchone()
        con.close()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def _group_umos() -> list[str]:
    """收集所有 `平台:GroupMessage:<uid>` 群会话（去重）。结果缓存 1 分钟，避免每次广播都全表扫。"""
    hit = _GROUP_UMOS_CACHE.get("_")
    if hit and time.time() - hit[0] < GROUP_UMOS_TTL:
        return list(hit[1])
    p = _data_root() / "data_v4.db"
    if not p.exists():
        return []
    umos: list[str] = []
    try:
        uri = "file:" + str(p).replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        cur = con.cursor()
        for (scope_id,) in cur.execute(
            "select scope_id from preferences where key='sel_conv_id' "
            "and scope_id like '%:GroupMessage:%'"
        ):
            if scope_id:
                umos.append(scope_id)
        try:
            for (user_id,) in cur.execute(
                "select distinct user_id from platform_message_history"
            ):
                uid = str(user_id or "")
                if ":GroupMessage:" in uid:
                    umos.append(uid)
        except Exception:
            pass
        con.close()
    except Exception:
        return []
    seen, out = set(), []
    for u in umos:
        if u not in seen:
            seen.add(u)
            out.append(u)
    _GROUP_UMOS_CACHE["_"] = (time.time(), out)
    return out


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

class Storage:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def _conn(self) -> sqlite3.Connection:
        con = _db_connect(str(_db_path()))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")   # 开机瞬间多个任务抢写时不直接抛 database is locked
        return con

    def init(self) -> None:
        con = self._conn()
        try:
            con.executescript(DDL)
            # 存量库迁移：新增 confirmed 列（历史数据默认视为已确认）
            cols = [row[1] for row in con.execute("PRAGMA table_info(members_funds)").fetchall()]
            if "confirmed" not in cols:
                con.execute("ALTER TABLE members_funds ADD COLUMN confirmed INTEGER DEFAULT 1")
            # 存量库迁移：新增 pending_amount 列（买入待确认挂账，默认0）
            if "pending_amount" not in cols:
                con.execute("ALTER TABLE members_funds ADD COLUMN pending_amount REAL DEFAULT 0")
            # 存量库迁移：新增 pending_principal 列（转换结转浮亏的成本本金基数，默认0=普通买入）
            if "pending_principal" not in cols:
                con.execute("ALTER TABLE members_funds ADD COLUMN pending_principal REAL DEFAULT 0")
            # 存量库迁移：新增 confirm_date 列（确认份额生效日，默认空=视为已过确认日，允许正常更新）
            cols = [row[1] for row in con.execute("PRAGMA table_info(members_funds)").fetchall()]
            if "confirm_date" not in cols:
                con.execute("ALTER TABLE members_funds ADD COLUMN confirm_date TEXT DEFAULT ''")
            # 存量库迁移：新增 shares 列（持有份额；>0 时现值=份额×净值，不再复利累乘）
            if "shares" not in cols:
                con.execute("ALTER TABLE members_funds ADD COLUMN shares REAL DEFAULT 0")
            cols = [row[1] for row in con.execute("PRAGMA table_info(members_funds)").fetchall()]
            # 存量库迁移：新增 realized_pnl 列（累计已实现盈亏，正=赚，负=亏）
            if "realized_pnl" not in cols:
                con.execute("ALTER TABLE members_funds ADD COLUMN realized_pnl REAL DEFAULT 0")
            # 存量库迁移：转换改成「按份额」——转出份额 / 转入净值 / 转入份额
            ccols = [row[1] for row in con.execute("PRAGMA table_info(conversions)").fetchall()]
            for _c, _d in (("out_shares", "REAL DEFAULT 0"), ("in_nav", "REAL DEFAULT 0"),
                           ("in_shares", "REAL DEFAULT 0")):
                if _c not in ccols:
                    con.execute(f"ALTER TABLE conversions ADD COLUMN {_c} {_d}")
            lcols = [row[1] for row in con.execute("PRAGMA table_info(pending_lots)").fetchall()]
            for _c, _d in (("shares", "REAL DEFAULT 0"), ("nav", "REAL DEFAULT 0")):
                if _c not in lcols:
                    con.execute(f"ALTER TABLE pending_lots ADD COLUMN {_c} {_d}")
            # 存量库迁移：早期版本把在途转换写成 status='pending'，会让它永远不被结算/看到
            con.execute("UPDATE conversions SET status='active' "
                        "WHERE status IS NULL OR status NOT IN ('active','settled')")
            # 预约表迁移为按条结构（member + fund_code，每条记录独立确认时间）
            scols = [row[1] for row in con.execute("PRAGMA table_info(confirm_schedules)").fetchall()]
            if "fund_code" not in scols:
                con.execute("DROP TABLE IF EXISTS confirm_schedules")
                con.execute(
                    "CREATE TABLE confirm_schedules("
                    "member TEXT NOT NULL, fund_code TEXT NOT NULL, confirm_at TEXT NOT NULL,"
                    " PRIMARY KEY (member, fund_code))")
            # 转换表迁移：新增 part_amount（部分转出金额；0=整只）
            ccols = [row[1] for row in con.execute("PRAGMA table_info(conversions)").fetchall()]
            if ccols and "part_amount" not in ccols:
                con.execute("ALTER TABLE conversions ADD COLUMN part_amount REAL DEFAULT 0")
            if ccols and "part_ratio" not in ccols:
                con.execute("ALTER TABLE conversions ADD COLUMN part_ratio REAL DEFAULT 0")
            con.commit()
        finally:
            con.close()

    def upsert_record(self, member: str, fund_code: str, fund_name: str,
                      amount: float, confirmed: int = 1) -> None:
        con = self._conn()
        try:
            today = time.strftime("%Y-%m-%d")
            con.execute(
                "INSERT INTO members_funds "
                "(member, fund_code, fund_name, principal, current, last_change, last_update, confirmed, confirm_date) "
                "VALUES (?,?,?,?,?,0,'',?,?) "
                "ON CONFLICT(member, fund_code) DO UPDATE SET "
                "principal=excluded.principal, current=excluded.current, "
                "fund_name=excluded.fund_name, last_change=0, last_update='', "
                "confirmed=excluded.confirmed, confirm_date=excluded.confirm_date",
                (member, fund_code, fund_name, amount, amount, confirmed, today),
            )
            con.commit()
        finally:
            con.close()

    def all_records(self) -> list[dict]:
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT member, fund_code, fund_name, principal, current, "
                "last_change, last_update, confirmed, pending_amount, shares, "
                "COALESCE(realized_pnl,0) "
                "FROM members_funds ORDER BY member, id"
            ).fetchall()
        finally:
            con.close()
        return [
            {"member": r[0], "fund_code": r[1], "fund_name": r[2],
             "principal": r[3], "current": r[4], "last_change": r[5],
             "last_update": r[6], "confirmed": r[7], "pending_amount": r[8],
             "shares": r[9], "realized_pnl": r[10]}
            for r in rows
        ]

    def pending_records(self) -> list[dict]:
        """返回所有待确认记录：整条待确认(confirmed=0) + 已确认在榜的买入挂账(pending_amount>0)。"""
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT member, fund_code, fund_name, principal, current, "
                "last_change, last_update, confirmed, pending_amount FROM members_funds "
                "WHERE confirmed=0 OR pending_amount>0 ORDER BY member, id"
            ).fetchall()
        finally:
            con.close()
        return [
            {"member": r[0], "fund_code": r[1], "fund_name": r[2],
             "principal": r[3], "current": r[4], "last_change": r[5],
             "last_update": r[6], "confirmed": r[7], "pending_amount": r[8]}
            for r in rows
        ]

    def confirm_member(self, member: str) -> int:
        """将某成员的所有待确认记录置为已确认，并把待确认买入挂账(pending_amount)并入本金。
        同时清空涨跌显示(last_change=0)：确认当刻不带涨跌，涨跌统一交给每日更新按确认日净值应用。
        返回更新的行数。"""
        con = self._conn()
        try:
            today = time.strftime("%Y-%m-%d")
            cur = con.execute(
                "UPDATE members_funds SET confirmed=1, confirm_date=?, last_change=0, "
                "principal=principal+CASE WHEN pending_principal>0 THEN pending_principal ELSE pending_amount END, "
                "current=current+pending_amount, pending_amount=0, pending_principal=0 "
                "WHERE member=? AND (confirmed=0 OR pending_amount>0)",
                (today, member),
            )
            n = cur.rowcount
            con.commit()
            return n
        finally:
            con.close()

    def confirm_member_codes(self, member: str, codes: list[str]) -> int:
        """只把某成员『指定基金代码』的待确认记录置为已确认，并把待确认买入挂账并入本金。
        返回更新的行数。用于预约到点确认：仅转正到点的那几条，不连带转正其它未到时间的待确认记录。
        同时清空涨跌显示(last_change=0)：待确认期间刷出的旧净值涨跌不带到确认后，
        涨跌统一交给每日更新（国内按确认日当晚净值、QDII 按自身净值日）应用。"""
        if not codes:
            return 0
        con = self._conn()
        try:
            today = time.strftime("%Y-%m-%d")
            # 该成员还有哪些「未到确认时间的转换批次」：这部分钱不能被通用确认提前转正
            lot_rows = con.execute(
                "SELECT fund_code, COALESCE(SUM(amount),0), COALESCE(SUM(principal),0) "
                "FROM pending_lots WHERE member=? AND status='pending' GROUP BY fund_code",
                (member,)).fetchall()
            lot_sum = {r[0]: (float(r[1] or 0.0), float(r[2] or 0.0)) for r in lot_rows}
            n = 0
            for code in codes:
                row = con.execute(
                    "SELECT pending_amount, pending_principal, confirmed FROM members_funds "
                    "WHERE member=? AND fund_code=?", (member, code)).fetchone()
                if not row:
                    continue
                pa = float(row[0] or 0.0)
                pp = float(row[1] or 0.0)
                if int(row[2] or 0) == 1 and pa <= 0 and pp <= 0:
                    continue  # 已经是已确认且没有挂账，空更新不计入条数
                la, lp = lot_sum.get(code, (0.0, 0.0))
                if la > 0 or lp > 0:
                    pa = max(round(pa - la, 2), 0.0)
                    pp = max(round(pp - lp, 2), 0.0)
                bucket = pp if pp > 0 else pa
                con.execute(
                    "UPDATE members_funds SET confirmed=1, confirm_date=?, last_change=0, "
                    "principal=principal+?, current=current+?, shares=0, "
                    "pending_amount=MAX(pending_amount-?,0), "
                    "pending_principal=MAX(pending_principal-?,0) "
                    "WHERE member=? AND fund_code=?",
                    (today, bucket, pa, pa, pp, member, code))
                n += 1
            con.commit()
            return n
        finally:
            con.close()


    def member_codes(self, member: str) -> list[str]:
        """返回某成员已确认记录的基金代码去重列表。"""
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT DISTINCT fund_code FROM members_funds "
                "WHERE member=? AND fund_code<>'' AND confirmed=1",
                (member,),
            ).fetchall()
        finally:
            con.close()
        return [r[0] for r in rows]

    def save_schedule(self, member: str, fund_code: str, confirm_at: str) -> None:
        con = self._conn()
        try:
            con.execute(
                "INSERT OR REPLACE INTO confirm_schedules(member, fund_code, confirm_at) VALUES(?,?,?)",
                (member, fund_code, confirm_at),
            )
            con.commit()
        finally:
            con.close()

    def load_schedules(self) -> dict[tuple[str, str], datetime]:
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT member, fund_code, confirm_at FROM confirm_schedules"
            ).fetchall()
        finally:
            con.close()
        out: dict[tuple[str, str], datetime] = {}
        for member, fc, at in rows:
            try:
                out[(member, fc)] = datetime.strptime(at, "%Y-%m-%d %H:%M")
            except ValueError:
                pass
        return out

    def remove_schedules(self, member: str, fund_code: str | None = None) -> None:
        con = self._conn()
        try:
            if fund_code:
                con.execute(
                    "DELETE FROM confirm_schedules WHERE member=? AND fund_code=?", (member, fund_code))
            else:
                con.execute("DELETE FROM confirm_schedules WHERE member=?", (member,))
            con.commit()
        finally:
            con.close()

    def set_pending(self, member: str, fund_code: str) -> None:
        """把某成员的一条记录改为待确认(confirmed=0)，资金回到本金，不参与每日更新。"""
        con = self._conn()
        try:
            con.execute(
                "UPDATE members_funds SET confirmed=0, current=principal, shares=0, "
                "last_change=0, last_update='', confirm_date='' WHERE member=? AND fund_code=?",
                (member, fund_code),
            )
            con.commit()
        finally:
            con.close()

    def distinct_codes(self) -> list[str]:
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT DISTINCT fund_code FROM members_funds WHERE fund_code<>''"
            ).fetchall()
        finally:
            con.close()
        return [r[0] for r in rows]

    def update_fund_day(self, code: str, change: float, nav_date: str,
                        nav: float = 0.0) -> int:
        """对某基金所有已确认、且『本次净值日 >= 确认日』的记录执行当日更新；
        对待确认(confirmed=0)记录只更新涨跌幅显示(last_change)与净值日，资金保持本金、不计入总资金。
        给了单位净值(nav)且记录有份额(shares) → 现值 = 份额 × 净值（不再按涨跌幅复利累乘，
        漏更新一天也不会丢涨跌）；没有份额时首次用『现值 ÷ 净值』反推并钉住份额。
        返回更新的已确认行数。"""
        con = self._conn()
        n = 0
        try:
            rows = con.execute(
                "SELECT id, current, shares FROM members_funds "
                "WHERE fund_code=? AND confirmed=1 AND last_update<>? "
                "AND (confirm_date IS NULL OR confirm_date='' OR confirm_date <= ?)",
                (code, nav_date, nav_date),
            ).fetchall()
            for rid, cur, sh in rows:
                sh = float(sh or 0.0)
                cur = float(cur or 0.0)
                if nav and nav > 0:
                    if sh <= 0 and cur > 0:
                        sh = round(cur / nav, 4)   # 首次：反推份额，现值保持不动
                    new = round(sh * nav, 2) if sh > 0 else cur
                else:
                    new = round(cur * (1.0 + change / 100.0), 2)
                con.execute(
                    "UPDATE members_funds SET current=?, last_change=?, "
                    "last_update=?, shares=? WHERE id=?",
                    (new, change, nav_date, sh, rid),
                )
                n += 1
            # 待确认：只更新涨跌显示与净值日，资金保持本金
            pend_rows = con.execute(
                "SELECT id FROM members_funds WHERE fund_code=? AND confirmed=0",
                (code,),
            ).fetchall()
            for (rid,) in pend_rows:
                con.execute(
                    "UPDATE members_funds SET last_change=?, last_update=? WHERE id=?",
                    (change, nav_date, rid),
                )
            con.commit()
        finally:
            con.close()
        return n

    def update_pending_change(self, code: str, change: float, nav_date: str) -> int:
        """只更新某基金待确认(confirmed=0)记录的涨跌显示(last_change)与净值日，资金保持本金、不计入总资金。
        用于普通基金当日净值未发布时，仍用最新可用净值刷新待确认部分的涨跌显示。返回更新的行数。"""
        con = self._conn()
        n = 0
        try:
            rows = con.execute(
                "SELECT id FROM members_funds WHERE fund_code=? AND confirmed=0",
                (code,),
            ).fetchall()
            for (rid,) in rows:
                con.execute(
                    "UPDATE members_funds SET last_change=?, last_update=? WHERE id=?",
                    (change, nav_date, rid),
                )
                n += 1
            con.commit()
        finally:
            con.close()
        return n

    def delete_record(self, member: str, fund_code: str) -> int:
        """删除某成员一条基金记录。返回删除行数。"""
        con = self._conn()
        try:
            cur = con.execute(
                "DELETE FROM members_funds WHERE member=? AND fund_code=?",
                (member, fund_code),
            )
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    def upsert_full(self, member: str, fund_code: str, fund_name: str,
                    principal: float, current: float) -> None:
        """对账写库：有则改，无则建，confirmed=1。
        只校准本金/现值，不动份额(shares)、涨跌显示(last_change)、净值日(last_update)，
        避免打乱待确认挂账、在途转换结算与当晚的净值更新。"""
        con = self._conn()
        try:
            row = con.execute(
                "SELECT id FROM members_funds WHERE member=? AND fund_code=?",
                (member, fund_code)).fetchone()
            if row:
                con.execute(
                    "UPDATE members_funds SET principal=?, current=?, fund_name=?, confirmed=1 "
                    "WHERE member=? AND fund_code=?",
                    (round(principal, 2), round(current, 2), fund_name or "", member, fund_code))
            else:
                con.execute(
                    "INSERT INTO members_funds(member,fund_code,fund_name,principal,current,"
                    "last_change,last_update,confirmed,confirm_date) VALUES(?,?,?,?,?,0,'',1,?)",
                    (member, fund_code, fund_name or "", round(principal, 2),
                     round(current, 2), time.strftime("%Y-%m-%d")))
            con.commit()
        finally:
            con.close()

    def set_values(self, member: str, fund_code: str, principal: float | None = None,
                   current: float | None = None, last_update: str = "",
                   preserve: bool = False) -> int:
        """只改指定的列（None 表示不动）。
        preserve=True（对账校准用）：不清份额/涨跌、不标净值日，避免打乱在途结算与当晚更新；
        默认(False)：现值被人工改写时作废份额(shares=0)并涨跌归零（供转换结算等用）。
        返回更新行数。"""
        sets, args = [], []
        if principal is not None:
            sets.append("principal=?")
            args.append(round(float(principal), 2))
        if current is not None:
            sets.append("current=?")
            args.append(round(float(current), 2))
            if not preserve:
                sets.append("shares=0")  # 现值被人工改写 → 份额作废，下次更新按净值重新反推
        if last_update and not preserve:
            sets.append("last_update=?")
            args.append(last_update)
        if not sets:
            return 0
        args.extend([member, fund_code])
        con = self._conn()
        try:
            cur = con.execute(
                "UPDATE members_funds SET " + ", ".join(sets) +
                ("" if preserve else ", last_change=0 ") +
                "WHERE member=? AND fund_code=?", args)
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    def update_principal(self, member: str, fund_code: str,
                         new_principal: float) -> int:
        """修改某成员一条基金记录的本金，同步重置现资金与涨跌。返回更新行数。"""
        con = self._conn()
        try:
            cur = con.execute(
                "UPDATE members_funds SET principal=?, current=?, shares=0, "
                "last_change=0 WHERE member=? AND fund_code=?",
                (new_principal, new_principal, member, fund_code),
            )
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    def buy_increase(self, member: str, fund_code: str, fund_name: str,
                     amount: float) -> tuple[int, bool]:
        """买入：已确认在榜的记录→买入金额挂账 pending_amount（已确认部分继续参与涨跌，未确认份额待转正）；
        整条待确认的记录→金额直接并入本金；无记录→新建一条待确认。
        返回 (影响行数, 是否新建)。"""
        con = self._conn()
        try:
            row = con.execute(
                "SELECT confirmed FROM members_funds WHERE member=? AND fund_code=?",
                (member, fund_code)).fetchone()
            if row:
                if row[0] == 1:
                    # 已确认在榜：买入份额挂账待确认，已确认部分照常参与涨跌
                    cur = con.execute(
                        "UPDATE members_funds SET pending_amount=pending_amount+?, fund_name=? "
                        "WHERE member=? AND fund_code=?",
                        (amount, fund_name, member, fund_code))
                else:
                    # 整条待确认：金额直接并入本金（仍在待确认）
                    cur = con.execute(
                        "UPDATE members_funds SET principal=principal+?, current=current+?, shares=0, fund_name=? "
                        "WHERE member=? AND fund_code=?",
                        (amount, amount, fund_name, member, fund_code))
                con.commit()
                n = cur.rowcount
            else:
                cur = con.execute(
                    "INSERT INTO members_funds "
                    "(member, fund_code, fund_name, principal, current, last_change, last_update, confirmed, pending_amount) "
                    "VALUES (?,?,?,?,?,0,'',0,0)",
                    (member, fund_code, fund_name, amount, amount))
                con.commit()
                n = cur.rowcount
            try:
                self.add_trade_log(member, fund_code, fund_name, "买入", amount)
            except Exception:
                pass  # 明细留痕失败不影响主流程
            return n, n > 0 and row is None
        finally:
            con.close()

    def get_fund(self, member: str, fund_code: str) -> dict | None:
        """取单条记录（买入快照 / 回滚参考用）。"""
        con = self._conn()
        try:
            row = con.execute(
                "SELECT member, fund_code, fund_name, principal, current, last_change, "
                "last_update, confirmed, pending_amount, confirm_date, shares "
                "FROM members_funds WHERE member=? AND fund_code=?",
                (member, fund_code)).fetchone()
            if not row:
                return None
            return {"member": row[0], "fund_code": row[1], "fund_name": row[2],
                    "principal": row[3], "current": row[4], "last_change": row[5],
                    "last_update": row[6], "confirmed": row[7], "pending_amount": row[8],
                    "confirm_date": row[9], "shares": float(row[10] or 0.0)}
        finally:
            con.close()

    def sell_decrease(self, member: str, fund_code: str, amount: float) -> tuple[int, float, float, float, str]:
        """卖出：amount=本次卖出到手金额。从已确认本金(principal)扣减对应成本、现资金(current)扣减到手额。

        按"卖出到手/当前现值"占比算出卖出部分的本金，本次盈亏 = 到手 − 卖出部分本金，
        累加到 realized_pnl（累计已实现盈亏，正=赚，负=亏），亏损不会丢失。
        仅允许卖出已确认的部分；待确认份额（整条 confirmed=0 或挂账 pending_amount）不受影响。
        返回 (影响行数, 卖出后剩余本金, 本次卖出盈亏, 该基金累计已实现盈亏, 错误信息或空串)。
        """
        con = self._conn()
        try:
            row = con.execute(
                "SELECT principal, current, confirmed, COALESCE(realized_pnl,0), "
                "COALESCE(shares,0) FROM members_funds "
                "WHERE member=? AND fund_code=?", (member, fund_code)).fetchone()
            if not row:
                return 0, 0.0, 0.0, 0.0, "找不到这条记录。"
            principal, current, confirmed = row[0], row[1], row[2]
            realized = float(row[3] or 0.0)
            shares0 = float(row[4] or 0.0)
            if confirmed != 1:
                return 0, principal, 0.0, realized, "这条还是待确认状态，份额确认后才能卖出（可「预约确认」或「确认份额」先转正）。"
            if principal <= 0:
                return 0, 0.0, 0.0, realized, "已确认本金为 0，没有可卖出的份额。"
            if amount <= 0:
                return 0, principal, 0.0, realized, "卖出金额需大于 0。"
            if amount > current + 1e-6:
                return 0, principal, 0.0, realized, f"卖出金额 ¥{amount:,.2f} 超过可卖现值 ¥{current:,.2f}，请重新输入。"
            # 计算卖出部分的本金与本次盈亏
            if amount >= current - 1e-6:          # 清仓/全部：到手=现值，卖出全部本金
                sold_p, sold_c = principal, current
                new_shares = 0.0
            else:                                 # 部分卖出：按到手/现值 占比分摊本金与份额
                ratio = amount / current
                sold_p = principal * ratio
                sold_c = amount
                new_shares = round(shares0 * (1.0 - ratio), 6) if shares0 > 0 else 0.0
            pnl = round(sold_c - sold_p, 2)
            new_p = max(0.0, principal - sold_p)
            new_c = max(0.0, current - sold_c)
            cur = con.execute(
                "UPDATE members_funds SET principal=?, current=?, shares=?, "
                "realized_pnl = ROUND(COALESCE(realized_pnl,0) + ?, 2) "
                "WHERE member=? AND fund_code=?",
                (new_p, new_c, new_shares, pnl, member, fund_code))
            con.commit()
            return cur.rowcount, new_p, pnl, round(realized + pnl, 2), ""
        finally:
            con.close()

    def freeze_sell(self, member: str, fund_code: str, fund_name: str,
                    shares: float = 0.0, expected: float = 0.0,
                    confirm_at: str = "", note: str = "") -> tuple[int, float, float, float, str]:
        """卖出挂单：把卖出的份额从持仓里冻结出去（扣份额/现值/本金），记一条 pending_sells。
        份额和预计金额都没有时只记挂单、不冻结（等到账截图再按比例扣）。
        返回 (成功, 冻结份额, 冻结现值, 对应成本, 错误)。"""
        con = self._conn()
        try:
            con.executescript(PENDING_SELLS_DDL)
            row = con.execute(
                "SELECT principal, current, confirmed, COALESCE(shares,0) FROM members_funds "
                "WHERE member=? AND fund_code=?", (member, fund_code)).fetchone()
            if not row:
                return 0, 0.0, 0.0, 0.0, "找不到这条记录。"
            principal, current, confirmed, total_sh = (
                float(row[0] or 0.0), float(row[1] or 0.0), int(row[2] or 0), float(row[3] or 0.0))
            if confirmed != 1:
                return 0, 0.0, 0.0, 0.0, "这条还是待确认状态，份额确认后才能卖出。"
            sh, exp, part, sold_c = 0.0, 0.0, 0.0, 0.0
            if shares > 0 and total_sh > 0:
                sh = min(float(shares), total_sh)
                ratio = sh / total_sh
                sold_c = round(current * ratio, 2)
                exp = round(float(expected or 0.0), 2) or sold_c
                part = round(principal * ratio, 2)
            elif expected > 0 and current > 0:
                exp = min(round(float(expected), 2), round(current, 2))
                ratio = exp / current
                sh = round(total_sh * ratio, 4)
                sold_c = exp
                part = round(principal * ratio, 2)
            if sh > 0:
                con.execute(
                    "UPDATE members_funds SET principal=?, current=?, shares=?, "
                    "realized_pnl=COALESCE(realized_pnl,0) WHERE member=? AND fund_code=?",
                    (round(max(principal - part, 0.0), 2), round(max(current - sold_c, 0.0), 2),
                     round(max(total_sh - sh, 0.0), 4), member, fund_code))
            con.execute(
                "INSERT INTO pending_sells(member, fund_code, fund_name, shares, expected, "
                "principal_part, actual, confirmed, confirm_at, notified, status, created) "
                "VALUES(?,?,?,?,?,?,0,0,?,0,'pending',?)",
                (member, fund_code, fund_name or "", round(sh, 4), exp, part,
                 confirm_at or "", time.strftime("%Y-%m-%d %H:%M:%S")))
            con.commit()
            return 1, round(sh, 4), round(sold_c, 2), part, ""
        finally:
            con.close()

    def pending_sells(self, member: str | None = None, fund_code: str | None = None,
                      status: str = "pending") -> list[dict]:
        con = self._conn()
        try:
            con.executescript(PENDING_SELLS_DDL)
            sql = ("SELECT id, member, fund_code, fund_name, shares, expected, principal_part, "
                   "actual, confirm_at, created FROM pending_sells WHERE status=?")
            args: list = [status]
            if member:
                sql += " AND member=?"; args.append(member)
            if fund_code:
                sql += " AND fund_code=?"; args.append(fund_code)
            sql += " ORDER BY id"
            rows = con.execute(sql, tuple(args)).fetchall()
        finally:
            con.close()
        keys = ("id", "member", "fund_code", "fund_name", "shares", "expected",
                "principal_part", "actual", "confirm_at", "created")
        return [dict(zip(keys, r)) for r in rows]

    def settle_sell(self, sell_id: int, actual: float) -> tuple[int, float, float, str]:
        """卖出到账结算：本次已实现 = 实际到手 − 对应成本本金，累加到 realized_pnl。
        principal_part<=0（挂单时没冻结）时返回 -1，由调用方改走 sell_decrease。
        返回 (成功, 本次盈亏, 该基金累计已实现, 错误)。"""
        con = self._conn()
        try:
            con.executescript(PENDING_SELLS_DDL)
            row = con.execute(
                "SELECT member, fund_code, principal_part, status FROM pending_sells WHERE id=?",
                (sell_id,)).fetchone()
            if not row or row[3] != "pending":
                return 0, 0.0, 0.0, "这条卖出挂单不存在或已结算。"
            member, code, part = row[0], row[1], float(row[2] or 0.0)
            actual = round(float(actual or 0.0), 2)
            if actual <= 0:
                return 0, 0.0, 0.0, "到账金额需大于 0。"
            if part <= 0:
                return -1, 0.0, 0.0, "该挂单没有冻结份额，按普通卖出处理。"
            pnl = round(actual - part, 2)
            con.execute(
                "UPDATE members_funds SET realized_pnl=ROUND(COALESCE(realized_pnl,0)+?,2) "
                "WHERE member=? AND fund_code=?", (pnl, member, code))
            con.execute(
                "UPDATE pending_sells SET status='done', confirmed=1, actual=? WHERE id=?",
                (actual, sell_id))
            con.commit()
            cum = con.execute(
                "SELECT COALESCE(realized_pnl,0) FROM members_funds "
                "WHERE member=? AND fund_code=?", (member, code)).fetchone()
            return 1, pnl, round(float(cum[0] or 0.0), 2), ""
        finally:
            con.close()

    def close_sell(self, sell_id: int, status: str = "done") -> int:
        """关闭一条卖出挂单（不结转盈亏，用于已被普通卖出覆盖的占位挂单）。"""
        con = self._conn()
        try:
            con.executescript(PENDING_SELLS_DDL)
            cur = con.execute(
                "UPDATE pending_sells SET status=?, confirmed=1 WHERE id=? AND status='pending'",
                (status, sell_id))
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    def add_trade_log(self, member: str, fund_code: str, fund_name: str,
                      op_type: str, amount: float) -> None:
        """写入一条交易记录（买入/卖出留痕）。"""
        con = self._conn()
        try:
            con.execute(
                "INSERT INTO trade_logs(member, fund_code, fund_name, op_type, amount, ts) "
                "VALUES(?,?,?,?,?,?)",
                (member, fund_code, fund_name or "", op_type, amount,
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            con.commit()
        finally:
            con.close()

    def trade_logs(self, member: str, limit: int = 30) -> list[dict]:
        """取某成员最近交易记录（时间倒序）。"""
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT member, fund_code, fund_name, op_type, amount, ts "
                "FROM trade_logs WHERE member=? ORDER BY id DESC LIMIT ?",
                (member, limit)).fetchall()
        finally:
            con.close()
        return [
            {"member": r[0], "fund_code": r[1], "fund_name": r[2],
             "op_type": r[3], "amount": r[4], "ts": r[5]}
            for r in rows
        ]

    # ---------- 在途转换（转出日之前继续跟净值，到转出日晚自动结算）----------
    def create_conversion(self, member: str, src_code: str, src_name: str,
                          tgt_code: str, tgt_name: str, out_date: str, confirm_at: str,
                          part_amount: float = 0.0, part_ratio: float = 0.0,
                          out_shares: float = 0.0) -> int:
        con = self._conn()
        try:
            cur = con.execute(
                "INSERT INTO conversions(member,src_code,src_name,tgt_code,tgt_name,out_date,"
                "confirm_at,part_amount,part_ratio,out_shares,status,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'active',?)",
                (member, src_code, src_name or "", tgt_code, tgt_name or "",
                 out_date, confirm_at or "", round(float(part_amount or 0.0), 2),
                 round(float(part_ratio or 0.0), 6),
                 round(float(out_shares or 0.0), 4),
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    def active_conversions(self, member: str | None = None) -> list[dict]:
        con = self._conn()
        try:
            if member:
                rows = con.execute(
                    "SELECT id,member,src_code,src_name,tgt_code,tgt_name,out_date,confirm_at,"
                    "part_amount,part_ratio,out_shares,in_nav,in_shares "
                    "FROM conversions WHERE status IN ('active','pending') AND member=?",
                    (member,)).fetchall()
            else:
                rows = con.execute(
                    "SELECT id,member,src_code,src_name,tgt_code,tgt_name,out_date,confirm_at,"
                    "part_amount,part_ratio,out_shares,in_nav,in_shares "
                    "FROM conversions WHERE status IN ('active','pending')").fetchall()
        finally:
            con.close()
        return [
            {"id": r[0], "member": r[1], "src_code": r[2], "src_name": r[3],
             "tgt_code": r[4], "tgt_name": r[5], "out_date": r[6], "confirm_at": r[7],
             "part_amount": float(r[8] or 0.0), "part_ratio": float(r[9] or 0.0),
             "out_shares": float(r[10] or 0.0), "in_nav": float(r[11] or 0.0),
             "in_shares": float(r[12] or 0.0)}
            for r in rows
        ]

    def recent_conversions(self, member: str, limit: int = 8) -> list[dict]:
        """最近的转换记录（含已结算），按 id 倒序。"""
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT id,member,src_code,src_name,tgt_code,tgt_name,out_date,"
                "confirm_at,amount,part_amount,part_ratio,out_shares,in_nav,in_shares,"
                "status,created FROM conversions "
                "WHERE member=? ORDER BY id DESC LIMIT ?", (member, int(limit))).fetchall()
        finally:
            con.close()
        keys = ("id", "member", "src_code", "src_name", "tgt_code", "tgt_name",
                "out_date", "confirm_at", "amount", "part_amount", "part_ratio",
                "out_shares", "in_nav", "in_shares", "status", "created")
        return [dict(zip(keys, r)) for r in rows]

    def get_meta(self, key: str) -> str:
        """读一条插件级状态（跨重启保留），如当天是否已发过更新通知。"""
        con = self._conn()
        try:
            row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
            return str(row[0] or "") if row else ""
        except Exception:
            return ""
        finally:
            con.close()

    def set_meta(self, key: str, value: str) -> None:
        con = self._conn()
        try:
            con.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                        "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value or "")))
            con.commit()
        finally:
            con.close()

    def settle_conversion_rows(self, member: str, src_code: str, tgt_code: str,
                               tgt_name: str, amount: float, basis: float | None = None,
                               zero_src: bool = True,
                               src_shares_out: float | None = None) -> None:
        """在途转换结算：把源基金的 amount/basis 转到转入基金。
        整只转出(zero_src=True)直接清零源；部分转出只扣减对应金额，剩余继续跟净值。
        浮亏结转：转入挂账同时记成本本金 basis，确认时按 basis 入本金、按 amount 入现值，
        这样源基金已实现的盈亏不会从榜的总盈亏里消失。"""
        if basis is None:
            basis = amount
        con = self._conn()
        try:
            if zero_src:
                con.execute(
                    "UPDATE members_funds SET principal=0, current=0, shares=0 "
                    "WHERE member=? AND fund_code=?",
                    (member, src_code))
            else:
                # 部分转出：现值按金额扣；份额优先按「申请转出份额」精确扣，没有份额才按金额比例扣。
                # 否则下次「份额×净值」会把转走的钱算回来。
                row_s = con.execute(
                    "SELECT current, shares FROM members_funds WHERE member=? AND fund_code=?",
                    (member, src_code)).fetchone()
                old_cur = float((row_s or [0, 0])[0] or 0.0)
                old_sh = float((row_s or [0, 0])[1] or 0.0)
                if src_shares_out is not None and src_shares_out > 0 and old_sh > 0:
                    new_sh = round(max(old_sh - src_shares_out, 0.0), 4)
                else:
                    new_sh = (round(old_sh * max(old_cur - amount, 0.0) / old_cur, 4)
                              if old_cur > 0 and old_sh > 0 else 0.0)
                con.execute(
                    "UPDATE members_funds SET principal=MAX(principal-?,0), "
                    "current=MAX(current-?,0), shares=? WHERE member=? AND fund_code=?",
                    (basis, amount, new_sh, member, src_code))
            # 转入挂账（已确认基金→pending；待确认/新建→并入）
            row = con.execute(
                "SELECT confirmed FROM members_funds WHERE member=? AND fund_code=?",
                (member, tgt_code)).fetchone()
            if row:
                if row[0] == 1:
                    con.execute(
                        "UPDATE members_funds SET pending_amount=pending_amount+?, "
                        "pending_principal=pending_principal+?, fund_name=? "
                        "WHERE member=? AND fund_code=?",
                        (amount, basis, tgt_name or "", member, tgt_code))
                else:
                    con.execute(
                        "UPDATE members_funds SET principal=principal+?, current=current+?, fund_name=? "
                        "WHERE member=? AND fund_code=?",
                        (basis, amount, tgt_name or "", member, tgt_code))
            else:
                # 转入基金原本没有：建一条待确认记录，钱全部记在 pending（与「买入新基金」同一口径），
                # 由转换批次到点确认时再并入本金/现值，避免确认时重复计入两遍。
                con.execute(
                    "INSERT INTO members_funds(member,fund_code,fund_name,principal,current,last_change,last_update,confirmed,pending_amount,pending_principal) "
                    "VALUES(?,?,?,0,0,0,'',0,?,?)",
                    (member, tgt_code, tgt_name or "", amount, basis))
            con.commit()
        finally:
            con.close()

    def mark_conversion_settled(self, cid: int, amount: float,
                                in_nav: float = 0.0, in_shares: float = 0.0) -> None:
        con = self._conn()
        try:
            con.execute("UPDATE conversions SET status='settled', amount=?, in_nav=?, "
                        "in_shares=? WHERE id=?",
                        (amount, round(float(in_nav or 0.0), 4),
                         round(float(in_shares or 0.0), 4), cid))
            con.commit()
        finally:
            con.close()

    def cancel_active_conversion(self, member: str) -> int:
        """取消某成员所有在途转换（源基金未动，直接删行即可）。返回删除条数。"""
        con = self._conn()
        try:
            cur = con.execute("DELETE FROM conversions WHERE member=? AND status IN ('active','pending')", (member,))
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    def cancel_conversion_by_id(self, cid: int) -> int:
        """只取消指定的一笔在途转换。返回删除条数。"""
        con = self._conn()
        try:
            cur = con.execute("DELETE FROM conversions WHERE id=? AND status IN ('active','pending')", (cid,))
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    # ---------- 待确认批次：转换转入的钱按笔记各自的确认时间 ----------
    def add_pending_lot(self, member: str, fund_code: str, fund_name: str, amount: float,
                        principal: float, confirm_at: str, conversion_id: int = 0,
                        shares: float = 0.0, nav: float = 0.0) -> int:
        """登记一个待确认批次（金额/成本本金/确认时间/转入份额各自独立）。
        shares/nav 是「按份额转换」算出来的转入份额与所用净值，确认时并入持仓份额。"""
        con = self._conn()
        try:
            cur = con.execute(
                "INSERT INTO pending_lots(member,fund_code,fund_name,amount,principal,"
                "confirm_at,conversion_id,shares,nav,notified,status,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,0,'pending',?)",
                (member, fund_code, fund_name or "", round(amount, 2), round(principal, 2),
                 (confirm_at or "").strip(), int(conversion_id or 0),
                 round(float(shares or 0.0), 4), round(float(nav or 0.0), 4),
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            con.commit()
            return int(cur.lastrowid or 0)
        finally:
            con.close()

    def pending_lots(self, member: str | None = None, fund_code: str | None = None,
                     status: str = "pending") -> list[dict]:
        con = self._conn()
        try:
            sql = ("SELECT id, member, fund_code, fund_name, amount, principal, confirm_at, "
                   "conversion_id, notified, status, created, COALESCE(shares,0), COALESCE(nav,0) "
                   "FROM pending_lots WHERE status=?")
            args: list = [status]
            if member:
                sql += " AND member=?"
                args.append(member)
            if fund_code:
                sql += " AND fund_code=?"
                args.append(fund_code)
            sql += " ORDER BY id"
            rows = con.execute(sql, tuple(args)).fetchall()
        finally:
            con.close()
        keys = ("id", "member", "fund_code", "fund_name", "amount", "principal",
                "confirm_at", "conversion_id", "notified", "status", "created",
                "shares", "nav")
        return [dict(zip(keys, r)) for r in rows]

    def lots_due(self, now_str: str, member: str | None = None,
                 codes: list[str] | None = None) -> list[dict]:
        """已到确认时间、仍待确认的批次（confirm_at 为空视为不预约，立即算到点）。"""
        out = []
        for lot in self.pending_lots(member=member):
            if codes and lot["fund_code"] not in codes:
                continue
            if (lot["confirm_at"] or "") and lot["confirm_at"] > now_str:
                continue
            out.append(lot)
        return out

    def mark_lot_notified(self, lot_id: int) -> None:
        con = self._conn()
        try:
            con.execute("UPDATE pending_lots SET notified=1 WHERE id=?", (lot_id,))
            con.commit()
        finally:
            con.close()

    def set_conversion_in(self, cid: int, in_nav: float, in_shares: float) -> None:
        """截图/手动给出「转入确认份额」时直接记在转换上，结算时不再按净值估算。"""
        con = self._conn()
        try:
            con.execute("UPDATE conversions SET in_nav=?, in_shares=? WHERE id=?",
                        (round(float(in_nav or 0.0), 4), round(float(in_shares or 0.0), 4), cid))
            con.commit()
        finally:
            con.close()

    def set_lot_shares(self, lot_id: int, shares: float, nav: float = 0.0) -> None:
        """把「按份额转换」算出的转入份额写到批次上，确认时并入持仓。"""
        con = self._conn()
        try:
            con.execute("UPDATE pending_lots SET shares=?, nav=? WHERE id=? AND status='pending'",
                        (round(float(shares or 0.0), 4), round(float(nav or 0.0), 4), lot_id))
            con.commit()
        finally:
            con.close()

    def reset_lot_notified(self, member: str) -> None:
        """提示被撤回/取消后重置，让到点检查下一轮重新提示。"""
        con = self._conn()
        try:
            con.execute("UPDATE pending_lots SET notified=0 WHERE member=? AND status='pending'",
                        (member,))
            con.commit()
        finally:
            con.close()

    def lot_sums(self, member: str) -> dict[str, tuple[float, float]]:
        """{fund_code: (未确认批次现值合计, 成本合计)}，用于把批次的钱挡在通用确认之外。"""
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT fund_code, COALESCE(SUM(amount),0), COALESCE(SUM(principal),0) "
                "FROM pending_lots WHERE member=? AND status='pending' GROUP BY fund_code",
                (member,)).fetchall()
        finally:
            con.close()
        return {r[0]: (float(r[1] or 0.0), float(r[2] or 0.0)) for r in rows}

    def confirm_lot(self, lot_id: int) -> int:
        """把某批次的钱从「待确认挂账」转成本金+现值，并标记该批次已确认。"""
        con = self._conn()
        try:
            row = con.execute(
                "SELECT member, fund_code, amount, principal, "
                "COALESCE(shares,0), COALESCE(nav,0) FROM pending_lots "
                "WHERE id=? AND status='pending'", (lot_id,)).fetchone()
            if not row:
                return 0
            member, code, amount, principal = row[0], row[1], row[2], row[3]
            lot_shares = float(row[4] or 0.0)
            amount = round(float(amount or 0.0), 2)
            principal = round(float(principal or 0.0), 2)
            bucket = principal if principal > 0 else amount
            if lot_shares > 0:
                # 按份额转换：把真实转入份额并入持仓（下次「份额×净值」就是这条基金的真实市值）
                con.execute(
                    "UPDATE members_funds SET confirmed=1, confirm_date=?, last_change=0, "
                    "principal=principal+?, current=current+?, "
                    "shares=COALESCE(shares,0)+?, "
                    "pending_amount=MAX(pending_amount-?,0), "
                    "pending_principal=MAX(pending_principal-?,0) "
                    "WHERE member=? AND fund_code=?",
                    (time.strftime("%Y-%m-%d"), bucket, amount, round(lot_shares, 4),
                     amount, principal, member, code))
            else:
                con.execute(
                    "UPDATE members_funds SET confirmed=1, confirm_date=?, last_change=0, "
                    "principal=principal+?, current=current+?, shares=0, "
                    "pending_amount=MAX(pending_amount-?,0), "
                    "pending_principal=MAX(pending_principal-?,0) "
                    "WHERE member=? AND fund_code=?",
                    (time.strftime("%Y-%m-%d"), bucket, amount, amount, principal, member, code))
            con.execute("UPDATE pending_lots SET status='confirmed' WHERE id=?", (lot_id,))
            con.commit()
            return 1
        finally:
            con.close()

    def undo_buy(self, member: str, fund_code: str, amount: float,
                 before_confirmed: int | None, was_new: bool) -> int:
        """撤销一次买入：was_new→删除新建记录；before_confirmed=1→回退 pending_amount；
        before_confirmed=0→回退 principal/current。返回影响行数。"""
        con = self._conn()
        try:
            if was_new:
                cur = con.execute(
                    "DELETE FROM members_funds WHERE member=? AND fund_code=?",
                    (member, fund_code))
            elif before_confirmed == 1:
                cur = con.execute(
                    "UPDATE members_funds SET pending_amount=MAX(pending_amount-?, 0) "
                    "WHERE member=? AND fund_code=?",
                    (amount, member, fund_code))
            else:
                cur = con.execute(
                    "UPDATE members_funds SET principal=MAX(principal-?,0), "
                    "current=MAX(current-?,0), shares=0 WHERE member=? AND fund_code=?",
                    (amount, amount, member, fund_code))
            con.commit()
            # 撤销买入：同步移除对应的买入留痕（同一成员/基金/金额的最近一条），保证交易明细真实
            try:
                con.execute(
                    "DELETE FROM trade_logs WHERE id IN ("
                    "SELECT id FROM trade_logs WHERE member=? AND fund_code=? "
                    "AND op_type='买入' AND amount=? ORDER BY id DESC LIMIT 1)",
                    (member, fund_code, amount))
                con.commit()
            except Exception:
                pass
            return cur.rowcount
        finally:
            con.close()


def _search_fund_code(name: str) -> str | None:
    """按基金名称查询天天基金代码。优先精确匹配，否则按名称相似度（偏好同类 A/C）。"""
    if not name:
        return None
    try:
        url = ("https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
               "?m=1&key=" + urllib.parse.quote(name))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return None
    rows = data.get("Datas") or []
    if not rows:
        return None
    target = name.strip()
    # 精确匹配
    for x in rows:
        if str(x.get("NAME", "")).strip() == target:
            return str(x.get("CODE", ""))
    # 相似度匹配，偏好末尾类别字母(A/C)一致
    import difflib
    best, best_sim = None, -1.0
    tail = target[-1].upper() if target and target[-1].upper() in ("A", "C") else ""
    for x in rows:
        cand = str(x.get("NAME", "")).strip()
        code = str(x.get("CODE", ""))
        if not cand or not code:
            continue
        sim = difflib.SequenceMatcher(None, target, cand).ratio()
        if tail and cand and cand[-1].upper() == tail:
            sim += 0.1
        if sim > best_sim:
            best_sim, best = sim, code
    return best if best_sim >= 0.5 else None


def _lookup_fund_name(code: str) -> str:
    """按 6 位代码查官方名称（查不到返回空串）。"""
    try:
        url = ("https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
               "?m=1&key=" + urllib.parse.quote(code))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return ""
    for x in (data.get("Datas") or []):
        if str(x.get("CODE", "")).strip() == code:
            return str(x.get("NAME", "")).strip()
    return ""


def _resolve_fund(text: str) -> tuple[str, str] | None:
    """把用户输入解析成 (代码, 名称)：纯 6 位数字直接当代码，否则按名称搜索。"""
    t = (text or "").strip()
    if not t:
        return None
    m = re.fullmatch(r"(\d{6})", t)
    if m:
        code = m.group(1)
        return code, (_lookup_fund_name(code) or code)
    c = _search_fund_code(t)
    if not c:
        return None
    return c, t


async def _llm_normalize_fund_names(names: list[str]) -> list[str]:
    """用当前视觉模型把 OCR 出来的噪声基金名规范化成标准全称（保持顺序）。"""
    o1 = _openai1_llm()
    if not o1 or not names:
        return [""] * len(names)
    base, key, model = o1
    prompt = (
        "以下是从基金持仓截图里识别出来的基金名称，可能含错别字、简称、截断或多余字。"
        "请把每一个规范化成标准的基金全称（要能被天天基金搜索命中，如『招商中证白酒指数(LOF)A』），"
        "顺序一一对应。若某个名称实在无法辨认成具体基金，输出空字符串。"
        "只输出严格 JSON 数组，如 [\"标准全称1\",\"标准全称2\"]，不要任何说明文字。\n"
        "名称列表：\n" + "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
    )
    payload = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        data = await _post_chat(base, key, payload, timeout=40)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = re.sub(r"```(?:json)?", "", content).strip().strip("`")
        arr = json.loads(content)
        if isinstance(arr, list) and arr:
            return [str(x or "").strip() for x in arr][:len(names)]
        return [""] * len(names)
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 名称规范化失败: {e}")
        return [""] * len(names)


def _fetch_source_a(code: str) -> tuple[float | None, str]:
    """源A：天天基金已公布单位净值涨跌幅（equityReturn）。"""
    try:
        req = urllib.request.Request(
            FUND_URL.format(code=code),
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://fund.eastmoney.com/"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", "ignore")
        m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
        if not m:
            return None, ""
        arr = json.loads(m.group(1))
        if not arr:
            return None, ""
        change = float(arr[-1].get("equityReturn") or 0.0)
        name = ""
        nm = re.search(r'fS_name\s*=\s*"([^"]*)"', text)
        if nm:
            name = nm.group(1)
        nav_date = ""
        x = arr[-1].get("x")
        if x:
            try:
                nav_date = datetime.fromtimestamp(x / 1000).strftime("%Y-%m-%d")
            except Exception:
                nav_date = ""
        return change, name, nav_date
    except Exception:
        return None, "", ""


def _fetch_source_b(code: str) -> tuple[float | None, str]:
    """源B：天天基金『当日估算净值涨跌幅』（pingzhongdata 的 Data_ACWorthTrend，
    替代已被反爬的 fundgz 估值接口）。基准取上一交易日净值。"""
    try:
        req = urllib.request.Request(
            FUND_URL.format(code=code),
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://fund.eastmoney.com/"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", "ignore")
        m = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
        if not m:
            return None, ""
        arr = json.loads(m.group(1))
        if len(arr) < 2:
            return None, ""
        est_now = float(arr[-1][1])
        today = datetime.now().strftime("%Y-%m-%d")
        est_ts = arr[-1][0]
        est_date = datetime.fromtimestamp(est_ts / 1000).strftime("%Y-%m-%d")
        if est_date != today:
            return None, ""  # 今日估算尚未生成，交由兜底用最近一次净值
        # 基准：上一交易日净值（netWorthTrend 中日期非今天的最后一项）
        base = None
        mn = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
        if mn:
            nav = json.loads(mn.group(1))
            for item in reversed(nav):
                x = item.get("x")
                d = datetime.fromtimestamp(x / 1000).strftime("%Y-%m-%d") if x else ""
                if d and d != today:
                    base = float(item.get("y") or 0.0)
                    break
        if base is None or base <= 0:
            base = float(arr[-2][1]) if arr[-2][1] else 0.0
        if not base or base <= 0:
            return None, ""
        chg = (est_now - base) / base * 100.0
        name = ""
        nm = re.search(r'fS_name\s*=\s*"([^"]*)"', text)
        if nm:
            name = nm.group(1)
        return chg, name
    except Exception:
        return None, ""


def _fetch_gszzl(code: str) -> tuple[float | None, str]:
    """查询基金『当日』涨跌幅（%）。当日净值已发布 → 用净值（最准）；未发布 → 用当日估算估值；
    估值不可用 → 兜底用最近一次净值（避免中断）。返回 (涨跌幅%, 基金名)；全部失败返回 (None, '')。"""
    today = datetime.now().strftime("%Y-%m-%d")
    fallback_a = None
    for attempt in range(3):
        a = _fetch_source_a(code)
        if a[0] is not None and a[2] == today:
            return a[0], a[1]
        if a[0] is not None and fallback_a is None:
            fallback_a = a
        b = _fetch_source_b(code)
        if b[0] is not None:
            return b[0], b[1]
        if attempt < 2:
            time.sleep(1)
    if fallback_a and fallback_a[0] is not None:
        return fallback_a[0], fallback_a[1]
    return None, ""


def _fetch_fund_name(code: str) -> str:
    """从 pingzhongdata 取基金名（fS_name）。失败返回 ''。"""
    try:
        req = urllib.request.Request(
            FUND_URL.format(code=code),
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://fund.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", "ignore")
        m = re.search(r'fS_name\s*=\s*"([^"]*)"', text)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _fetch_nav_info(code: str) -> tuple[str, float | None, str]:
    """联网获取基金最新确认净值：返回 (最新净值日期 YYYY-MM-DD, 当日涨跌幅%, 基金名)。
    优先用天天基金 f10 历史净值接口（更新快，当日确认净值发布后立即可取）；
    失败回退 pingzhongdata。"""
    # 源A: f10 历史净值接口（权威确认净值，更新快）
    try:
        url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=" + code +
               "&pageIndex=1&pageSize=2&_=" + str(int(time.time() * 1000)))
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0",
                          "Referer": "https://fundf10.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        ls = (data.get("Data") or {}).get("LSJZList") or []
        if ls:
            row = ls[0]
            nav_date = row.get("FSRQ") or ""
            name = ((data.get("Data") or {}).get("FUNDNAME")) or _fetch_fund_name(code)
            raw_chg = str(row.get("JZZZL") or "").strip().replace("%", "")
            change: float | None = None
            if raw_chg not in ("", "-", "None"):
                try:
                    change = float(raw_chg)
                except (TypeError, ValueError):
                    change = None
            if change is None:
                # 净值日已更新、但涨跌幅还没算出来（当日净值刚出时常见）→ 用前后两日净值自己算。
                # 绝不能把空值当 0.00%：否则当天记为「涨跌 0」并写死 last_update，
                # 真实涨跌再也没有机会补上（今天 002053 -4.55% 就是这么丢的）。
                nav0 = float(row.get("DWJZ") or 0.0)
                nav1 = float(ls[1].get("DWJZ") or 0.0) if len(ls) > 1 else 0.0
                if nav0 > 0 and nav1 > 0:
                    change = round((nav0 / nav1 - 1.0) * 100.0, 4)
                else:
                    return nav_date, None, name  # 让调用方跳过，下一轮再来
            return nav_date, change, name
    except Exception:
        pass
    # 源B: pingzhongdata 回退（更新可能滞后）
    try:
        req = urllib.request.Request(
            FUND_URL.format(code=code),
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://fund.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", "ignore")
        m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
        if m:
            arr = json.loads(m.group(1))
            if arr:
                last = arr[-1]
                x = last.get("x")
                nav_date = datetime.fromtimestamp(x / 1000).strftime("%Y-%m-%d") if x else ""
                change = float(last.get("equityReturn") or 0.0)
                name = ""
                nm = re.search(r'fS_name\s*=\s*"([^"]*)"', text)
                if nm:
                    name = nm.group(1)
                return nav_date, change, name
    except Exception:
        pass
    return "", None, ""


def _fetch_latest_nav(code: str) -> tuple[str, float] | None:
    """取最新单位净值 (日期, 净值)。用于把「份额」换算成金额（转换订单登记）。"""
    try:
        url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=" + code +
               "&pageIndex=1&pageSize=1&_=" + str(int(time.time() * 1000)))
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0",
                          "Referer": "https://fundf10.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        ls = (data.get("Data") or {}).get("LSJZList") or []
        if ls:
            d = str(ls[0].get("FSRQ") or "")
            nav = float(ls[0].get("DWJZ") or 0.0)
            if d and nav > 0:
                return d, nav
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 取最新净值失败 {code}: {e}")
    return None


def _fetch_nav_on(code: str, date: str) -> float | None:
    """取某只基金在指定净值日(YYYY-MM-DD)的单位净值；那天没有净值（未出/非交易日）返回 None。
    按份额转换要靠它把「转出份额 / 转入金额」换成准确的「转入份额」。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", (date or "").strip()):
        return None
    try:
        url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=" + code +
               "&pageIndex=1&pageSize=40&_=" + str(int(time.time() * 1000)))
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0",
                          "Referer": "https://fundf10.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        for row in ((data.get("Data") or {}).get("LSJZList") or []):
            if str(row.get("FSRQ") or "") == date.strip():
                nav = float(row.get("DWJZ") or 0.0)
                return nav if nav > 0 else None
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 取 {code} {date} 净值失败: {e}")
    return None


def _fetch_nav_full(code: str) -> tuple[str, float | None, float, str]:
    """(净值日, 涨跌%, 单位净值, 基金名)。单位净值取不到时返回 0（调用方回退按涨跌幅复利）。
    涨跌幅为空（当日净值刚出、还没算涨跌）时用「本日净值 ÷ 前一日净值」自己算，
    绝不把空值当 0.00% —— 否则当天会被记成「涨跌 0」并写死 last_update，真实涨跌再也没机会补上。"""
    try:
        url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=" + code +
               "&pageIndex=1&pageSize=2&_=" + str(int(time.time() * 1000)))
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0",
                          "Referer": "https://fundf10.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        ls = (data.get("Data") or {}).get("LSJZList") or []
        if ls:
            row = ls[0]
            nav_date = str(row.get("FSRQ") or "")
            name = ((data.get("Data") or {}).get("FUNDNAME")) or ""
            nav = 0.0
            try:
                nav = float(row.get("DWJZ") or 0.0)
            except (TypeError, ValueError):
                nav = 0.0
            raw_chg = str(row.get("JZZZL") or "").strip().replace("%", "")
            change: float | None = None
            if raw_chg not in ("", "-", "None"):
                try:
                    change = float(raw_chg)
                except (TypeError, ValueError):
                    change = None
            if change is None:
                try:
                    prev_nav = float(ls[1].get("DWJZ") or 0.0) if len(ls) > 1 else 0.0
                except (TypeError, ValueError):
                    prev_nav = 0.0
                if nav > 0 and prev_nav > 0:
                    change = round((nav / prev_nav - 1.0) * 100.0, 4)
                else:
                    return nav_date, None, nav, name
            return nav_date, change, nav, name
    except Exception:
        pass
    d, c, nm = _fetch_nav_info(code)
    return d, c, 0.0, nm


def _fund_confirm_rule(name: str) -> str:
    """按基金名称推断确认份额所需交易日 T+N（场外基金）。"""
    n = name or ""
    if "QDII" in n:
        return "QDII · 确认 T+2 交易日（15:00后申购顺延 T+3）"
    if "货币" in n or "现金管理" in n:
        return "货币基金 · 确认 T+1（部分 T+0）"
    if "债" in n:
        return "债券基金 · 确认 T+1 交易日"
    if "混合" in n or "股票" in n:
        return "权益类(混合/股票) · 确认 T+1 交易日"
    if "ETF" in n or "联接" in n:
        return "ETF联接 · 确认 T+1 交易日（商品/黄金 T+1~T+2）"
    return "常规 · 确认 T+1 交易日"


# ---------------------------------------------------------------------------
# 图像识别导入
# ---------------------------------------------------------------------------

def _resolve_image_file(comp) -> str | None:
    conv = getattr(comp, "convert_to_file_path", None)
    if callable(conv):
        try:
            path = conv()
            if isinstance(path, str) and path and os.path.exists(path):
                return path
        except Exception:
            pass
    for attr in ("file", "path", "url"):
        val = getattr(comp, attr, None)
        if not val:
            continue
        val = str(val)
        if val.startswith("http://") or val.startswith("https://"):
            return _download(val)
        if val.startswith("base64://"):
            try:
                data = base64.b64decode(val[len("base64://"):])
                fd, p = _temp("img")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                return p
            except Exception:
                continue
        if val.startswith("data:"):
            try:
                b64 = val.split(",", 1)[-1]
                data = base64.b64decode(b64)
                fd, p = _temp("img")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                return p
            except Exception:
                continue
        if os.path.exists(val):
            return val
    return None


def _temp(suffix: str) -> tuple[int, str]:
    import tempfile
    fd, path = tempfile.mkstemp(suffix="." + suffix)
    return fd, path


def _download(url: str) -> str | None:
    fd, path = _temp("img")
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp, open(path, "wb") as f:
            f.write(resp.read())
        if os.path.getsize(path) == 0:
            os.remove(path)
            return None
        return path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        return None


async def _ocr_image(image_path: str) -> list[dict]:
    """调用当前视觉模型 多模态模型识别截图，返回记录列表。"""
    o1 = _openai1_vision()
    if not o1:
        raise RuntimeError("未找到可用的 deepseek-v4-flash-vision 配置，请检查 AstrBot provider")
    base, key, model = o1

    mime = "png"
    ext = os.path.splitext(image_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        mime = "jpeg"
    elif ext == ".gif":
        mime = "gif"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
                ],
            }
        ],
    }
    data = await _post_chat(base, key, payload, timeout=60)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return _extract_records(content)


CONVERT_ORDER_PROMPT = (
    "这是一张支付宝/理财 App 的基金交易截图（可能是「基金转换」下单页、订单详情页、"
    "或转换完成的记录页）。先判断类型再提取，输出严格 JSON：\n"
    '{"kind":"convert|holdings|other","src_name":"","src_code":"",'
    '"tgt_name":"","tgt_code":"","shares":数字或null,"amount":数字或null,'
    '"out_nav":数字或null,"in_nav":数字或null,"in_shares":数字或null,"fee":数字或null,'
    '"apply_time":"YYYY-MM-DD HH:MM:SS","out_confirm_by":"YYYY-MM-DD",'
    '"in_confirm_by":"YYYY-MM-DD"}\n'
    "规则：\n"
    "1) 「基金转换」相关页面（有转出基金/转入基金/申请转出份额/转换确认）→ kind=convert。\n"
    "2) 持仓列表页 → kind=holdings。其它 → kind=other。\n"
    "3) src=转出基金、tgt=转入基金，名称照抄截图；截图上没有代码就留空字符串。\n"
    "4) shares=申请转出份额（只要数字，不要带「份」字）；amount=转出/转入金额（没有就 null）。\n"
    "5) 转换是按份额的：out_nav=转出净值（转出日单位净值），"
    "in_nav=转入净值（确认日单位净值），in_shares=确认后的转入份额；"
    "界面没给就填 null，不要猜。\n"
    "6) fee=转换费/手续费（没有就 null）。\n"
    "7) apply_time=申请时间；out_confirm_by/in_confirm_by=预计完成转出/转入确认的日期。\n"
    "8) 「份额」和「金额」是两个不同的数，绝不要把份额当成金额填到 amount。\n"
    "只输出 JSON，不要解释。"
)


async def _ocr_convert_order(image_path: str) -> dict:
    """识别「转换订单」截图，返回结构化字段；不是转换单返回 {}。"""
    o1 = _openai1_vision()
    if not o1:
        return {}
    base, key, model = o1
    mime = "png"
    ext = os.path.splitext(image_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        mime = "jpeg"
    elif ext == ".gif":
        mime = "gif"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": CONVERT_ORDER_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
        ]}],
    }
    try:
        data = await _post_chat(base, key, payload, timeout=60)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = re.sub(r"```(?:json)?", "", content).strip().strip("`")
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else {}
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 转换订单识别失败: {e}")
        return {}


ORDER_OP_PROMPT = (
    "这是一张支付宝/理财 App 的基金交易截图（买入/申购/定投、卖出/赎回、"
    "转换、持仓列表、或其他页面）。先判断 kind 再提取，输出严格 JSON：\n"
    '{"kind":"buy|sell|convert|holdings|other",'
    '"fund_name":"","fund_code":"",'
    '"amount":数字或null,"shares":数字或null,'
    '"apply_time":"YYYY-MM-DD HH:MM:SS",'
    '"status":"确认中|金额待确认|已确认|已到账|其他",'
    '"confirm_by":"YYYY-MM-DD"}\n'
    "规则：\n"
    "1) 买入/申购/定投下单页或记录 → kind=buy；卖出/赎回/清仓记录 → kind=sell；"
    "转出转入的转换单 → kind=convert；持仓列表（多只基金金额）→ holdings；无关页面 → other。\n"
    "2) 单只基金交易记录：fund_name 照抄截图，fund_code 为 6 位数字（截图没有就留空字符串）。\n"
    "3) amount=交易金额（元，申购金额或卖出到手金额）；shares=份额；两者是不同的数，"
    "绝不要把份额填进 amount；界面没给就填 null。\n"
    "4) status 照抄页面状态文案（如「金额待确认」「确认中」「已确认」「已到账」）；"
    "apply_time=申请/交易时间；confirm_by=预计确认或到账日期（没有就留空）。\n"
    "只输出 JSON，不要解释。"
)


async def _ocr_order_op(image_path: str) -> dict:
    """识别买入/卖出/转换/持仓交易截图，返回结构化字段；无法判定返回 {}。"""
    o1 = _openai1_vision()
    if not o1:
        return {}
    base, key, model = o1
    mime = "png"
    ext = os.path.splitext(image_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        mime = "jpeg"
    elif ext == ".gif":
        mime = "gif"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": ORDER_OP_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
        ]}],
    }
    try:
        data = await _post_chat(base, key, payload, timeout=60)
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        content = re.sub(r"```(?:json)?", "", content).strip().strip("`")
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else {}
    except Exception as e:
        logger.warning(f"[羊村基金涨跌榜] 交易截图识别失败: {e}")
        return {}


async def _resolve_image_async(comp) -> str | None:
    """优先 await comp.convert_to_file_path()（新版 AstrBot 是协程），再退回同步解析。
    直接调同步版会拿到 coroutine 而路径为空，导致「拿不到截图内容」。"""
    conv = getattr(comp, "convert_to_file_path", None)
    if callable(conv):
        try:
            r = conv()
            if hasattr(r, "__await__"):
                r = await r
            if isinstance(r, str) and r and os.path.exists(r):
                return r
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 异步取图失败: {e}")
    return _resolve_image_file(comp)


def _extract_records(content: str) -> tuple[list[dict], str, str]:
    """解析 OCR 输出。返回 (记录列表, 数据日期, 截图类型)。"""
    # 去掉可能的 markdown 代码块
    content = re.sub(r"```(?:json)?", "", content).strip().strip("`")
    try:
        obj = json.loads(content)
    except Exception:
        # 尝试从内容中抓出第一个 JSON 对象
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return [], "", ""
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return [], "", ""
    date = ""
    page_type = ""
    if isinstance(obj, dict):
        date = str(obj.get("date", "") or "").strip()
        page_type = str(obj.get("page_type", "") or "").strip()
    records = obj.get("records") if isinstance(obj, dict) else obj
    if not isinstance(records, list):
        return [], date, page_type
    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        member = str(rec.get("member", "") or "").strip()
        code = str(rec.get("fund_code", "") or "").strip()
        fname = str(rec.get("fund_name", "") or "").strip()
        try:
            amount = float(str(rec.get("amount", "") or "").replace(",", "").strip())
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0 or (not code and not fname):
            continue
        code = re.sub(r"\D", "", code)[:6]
        out.append({"member": member, "fund_code": code,
                    "fund_name": fname, "amount": amount})
    return out, date, page_type


# ---------------------------------------------------------------------------
# 主插件
# ---------------------------------------------------------------------------

class Main(Star):
    def __init__(self, context: Star.Context, config=None) -> None:
        super().__init__(context)
        self._config = config if config is not None else {}
        global _DEFAULT_CONFIRM_TIME
        c = self._config
        def _cint(k, d):
            try:
                return int(c.get(k, d))
            except (TypeError, ValueError):
                return d
        def _cbool(k, d):
            v = c.get(k, d)
            return v if isinstance(v, bool) else d
        _dt = str(c.get("daily_update_time", "20:00")).strip() or "20:00"
        self.cfg = {
            "default_confirm_time": str(c.get("default_confirm_time", "12:00")).strip() or "12:00",
            "ocr_strict_page": _cbool("ocr_strict_page", True),
            "daily_update_time": _dt,
            "board_row_h": _cint("board_row_h", 96),
            "member_funds": c.get("member_funds") or [],
        }
        _DEFAULT_CONFIRM_TIME = self.cfg["default_confirm_time"]
        try:
            self._upd_hour, self._upd_min = int(_dt[:2]), int(_dt[3:5])
        except Exception:
            self._upd_hour, self._upd_min = UPDATE_HOUR, UPDATE_MIN
        self._storage = Storage()
        self._timer_task: asyncio.Task | None = None
        self._estimate_task: asyncio.Task | None = None
        self._pending: dict[tuple[str, str], dict] = {}  # (umo, sender) -> 待确认记录
        self._schedule_select: dict[tuple[str, str], dict] = {}  # (umo,sender) -> 预约选择状态
        self._edit_select: dict[tuple[str, str], str] = {}  # (umo,sender) -> "delete"/"modify"
        self._edit_records: dict[tuple[str, str], list] = {}  # (umo,sender) -> 记录列表
        self._buy_select: dict[tuple[str, str], dict] = {}  # (umo,sender) -> 买入选择状态 {member,records,prompt_mid}
        self._buy_confirm: dict[tuple[str, str], dict] = {}  # (umo,sender) -> 买入后确认时间状态 {member,code,name,amount,before_confirmed,was_new,prompt_mid}
        self._sell_select: dict[tuple[str, str], dict] = {}  # (umo,sender) -> 卖出选择状态 {member,records,prompt_mid}
        self._convert_select: dict[tuple[str, str], dict] = {}  # (umo,sender) -> 基金转换流程 {member,stage,records,...}
        self._holdings_select: dict[tuple[str, str], dict] = {}  # (umo,sender) -> 重仓股查询 {member,records}
        self._recon_pending: dict[tuple[str, str], dict] = {}  # (umo,sender) -> 对账待写入 {member,ops,...}
        self._order_select: dict[tuple[str, str], dict] = {}  # (umo,sender) -> 买/卖截图确认方案 {member,op,code,name,amount,shares,status,apply_time,confirm_by}
        self._bot_api = None  # 缓存的 QQ 官方 bot api（定时撤回提示消息用）
        self._state_ts: dict[tuple[str, str], dict] = {}  # (umo,sender) -> {"pending":ts,"schedule":ts,"edit":ts}
        self._storage.init()
        # 从 config 的 member_funds 同步成员基金数据到库（新增缺失；已存在仅本金不同才更新）
        try:
            self._sync_member_funds_sync()
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 成员基金同步失败: {e}")
        self._confirm_schedule: dict[tuple[str, str], datetime] = self._storage.load_schedules()  # (member,fund_code) -> 预约确认时间（持久化，按条独立）
        self._pending_confirm: dict[str, dict] = {}  # member -> {"mids": [...], "codes": [...]} 预约到点等待本人回复确认后才转正并撤回
        self._notified_day = ""  # 当天是否已发过「净值已更新」通知（一天只发一次，避免多轮补齐刷屏）
        logger.info(f"[羊村基金涨跌榜] 插件 v{PLUGIN_VERSION} 已加载 config={self.cfg}")
        try:
            self._register_web_apis()
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 注册 Web API 失败: {e}")
        # 插件热重载时 on_astrbot_loaded 不会再触发，这里兜底拉起定时任务，
        # 否则重载后每日更新与预约到点检查会静默停摆。
        self._ensure_scheduler()

    @filter.llm_tool(name="query_fund_db")
    async def query_fund_db(self, event, member: str = "", fund_code: str = "") -> str:
        """只读查询羊村基金涨跌榜的群成员基金持仓数据（本金、当前资金、涨跌、确认状态、确认日、预约确认），用于回答群内基金问题。本工具只读，绝不修改任何数据。

        Args:
            member(string): 成员昵称，不传则返回全部成员。
            fund_code(string): 基金代码（6位），不传则返回全部基金。
        """
        m = str(member or "").strip()
        code = re.sub(r"\D", "", str(fund_code or ""))[:6]
        rows = self._storage.all_records()
        if m:
            # 群昵称常带 emoji/后缀（成员昵称），台账里是 成员昵称；不能精确等值，
            # 否则 LLM 调工具会得到“无记录”，再对用户乱答“工具报错”。
            rows = [r for r in rows if self._member_matches(r["member"], m)]
        if code:
            rows = [r for r in rows if r["fund_code"] == code]
        if not rows:
            return f"羊村基金数据库中无{('成员『' + m + '』' if m else '')}{('基金 ' + code if code else '')}的记录。"
        # 只读额外读取确认日与预约计划（mode=ro，绝不写库）
        confirm_dates, sched = {}, self._storage.load_schedules()
        try:
            import sqlite3 as _sq
            con = _sq.connect(f"file:{_db_path()}?mode=ro", uri=True)
            try:
                for r in con.execute("SELECT member, fund_code, confirm_date FROM members_funds").fetchall():
                    confirm_dates[(r[0], r[1])] = r[2] or ""
            finally:
                con.close()
        except Exception:
            confirm_dates = {}
        by_member: dict = {}
        for r in rows:
            by_member.setdefault(r["member"], []).append(r)
        lines = []
        for mm in sorted(by_member):
            lines.append(f"成员 {mm}:")
            for r in sorted(by_member[mm], key=lambda x: x["fund_code"]):
                status = "已确认" if r["confirmed"] else "待确认(未转正)"
                cd = confirm_dates.get((r["member"], r["fund_code"]), "")
                lines.append(
                    f"  - {r['fund_name'] or r['fund_code']}({r['fund_code']}): "
                    f"本金 {r['principal']:.2f}, 现资金 {r['current']:.2f}, "
                    f"涨跌 {r['last_change']:+.2f}%, 更新日 {r['last_update'] or '-'}, "
                    f"状态: {status}" + (f", 确认日 {cd}" if cd else "")
                )
                if r["pending_amount"]:
                    lines.append(f"      未确认挂账 +{r['pending_amount']:.2f}")
            for (sm, sc), dt in sorted(sched.items()):
                if sm == mm and (not code or sc == code):
                    lines.append(f"      预约确认: 基金 {sc} 于 {dt.strftime('%Y-%m-%d %H:%M')}")
        return "\n".join(lines)

    @filter.llm_tool(name="fetch_fund_data")
    async def fetch_fund_data(self, event, fund_code: str = "") -> str:
        """联网只读查询一只基金的实时数据：名称、最新净值、最新单日涨跌、近1/3月累计涨跌、同类排名百分位、规模。用于基金诊断时获取实时行情。本工具只读，不修改任何数据。

        Args:
            fund_code(string): 6位基金代码，必填。
        """
        code = re.sub(r"\D", "", str(fund_code or ""))[:6]
        if len(code) != 6:
            return "请提供 6 位基金代码，例如 fetch_fund_data(fund_code='000001')。"

        def _cum(arr, n):
            p = 1.0
            for v in arr[-n:]:
                try:
                    p *= (1 + float(v.get("equityReturn", 0)) / 100)
                except (TypeError, ValueError):
                    pass
            return (p - 1) * 100

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                pz = (await client.get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js")).text
        except Exception as e:
            return f"{code}: 联网查询失败 ({e})"
        lines = []
        nm = re.search(r"fS_name\s*=\s*\"([^\"]+)\"", pz)
        if nm:
            lines.append(f"基金名称: {nm.group(1)} ({code})")
        tm = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", pz, re.S)
        if tm:
            arr = json.loads(tm.group(1))
            if arr:
                lines.append(f"最新单位净值: {arr[-1].get('y', '-')}")
                lines.append(f"最新净值日涨跌: {arr[-1].get('equityReturn', '-')}%")
                lines.append(f"近1月累计涨跌: {_cum(arr, 22):.2f}%  近3月累计涨跌: {_cum(arr, 64):.2f}%")
            else:
                lines.append(f"{code}: 暂无净值数据")
        else:
            lines.append(f"{code}: 未解析到净值趋势")
        rm = re.search(r"Data_rateInSimilarType\s*=\s*(\[.*?\]);", pz, re.S)
        if rm:
            rarr = json.loads(rm.group(1))
            if rarr:
                last = rarr[-1]
                rk, sc = last.get("y"), last.get("sc")
                try:
                    pct = float(rk) / float(sc) * 100
                    lines.append(f"同类排名: {rk}/{sc} (百分位约 {pct:.0f}%, 越小越靠前)")
                except (TypeError, ValueError, ZeroDivisionError):
                    lines.append(f"同类排名: {rk}/{sc}")
        return "\n".join(lines) or f"{code}: 未获取到数据"

    def _sync_member_funds_sync(self) -> None:
        """把 config.member_funds 同步进数据库：
        库中没有的 (member, fund_code) 新增；已有的仅当本金值不同才更新本金（现资金随之重置）。
        彻底删除记录请走群内「删除基金」指令。初始化阶段调用，无并发，不加锁。"""
        cfg_funds = self.cfg.get("member_funds") or []
        if not cfg_funds:
            return
        existing = {(r["member"], r["fund_code"]): r for r in self._storage.all_records()}
        added = updated = 0
        for f in cfg_funds:
            if not isinstance(f, dict):
                continue
            m = str(f.get("member") or "").strip()
            code = re.sub(r"\D", "", str(f.get("fund_code") or ""))[:6]
            name = str(f.get("fund_name") or "").strip() or code
            try:
                amt = float(str(f.get("principal") or f.get("amount") or "0")
                            .replace(",", "").strip())
            except (TypeError, ValueError):
                amt = 0.0
            if not m or not code or amt <= 0:
                continue
            key = (m, code)
            if key in existing:
                if abs(float(existing[key]["principal"]) - amt) > 1e-6:
                    self._storage.update_principal(m, code, amt)
                    updated += 1
            else:
                self._storage.upsert_record(m, code, name, amt, confirmed=1)
                added += 1
        if added or updated:
            logger.info(f"[羊村基金涨跌榜] config 成员基金同步: 新增{added} 更新{updated}")

    # ---------- 插件页面 Web API（AstrBot 自带「插件页面」数据管理） ----------

    def _register_web_apis(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            return
        prefix = "/astrbot_plugin_sheep_fund_rank/api"
        self.context.register_web_api(f"{prefix}/summary", self._web_summary, ["GET"], "涨跌榜总览")
        self.context.register_web_api(f"{prefix}/records", self._web_records, ["GET"], "成员基金明细")
        self.context.register_web_api(f"{prefix}/records", self._web_add, ["POST"], "新增成员基金")
        self.context.register_web_api(f"{prefix}/records/<rid>/update", self._web_update, ["POST"], "修改成员基金")
        self.context.register_web_api(f"{prefix}/records/<rid>/delete", self._web_delete, ["POST"], "删除成员基金")
        self.context.register_web_api(f"{prefix}/records/<rid>/state", self._web_record_state, ["POST"], "切换记录确认状态")
        self.context.register_web_api(f"{prefix}/schedules", self._web_schedules, ["GET"], "预约确认计划列表")
        self.context.register_web_api(f"{prefix}/schedules", self._web_schedule_upsert, ["POST"], "新增/更新预约确认")
        self.context.register_web_api(f"{prefix}/schedules/<member>/delete", self._web_schedule_delete, ["POST"], "删除预约确认")
        self.context.register_web_api(f"{prefix}/schedules/<member>/<fund_code>/delete", self._web_schedule_delete_one, ["POST"], "删除单条预约确认")
        self.context.register_web_api(f"{prefix}/members", self._web_members, ["GET"], "群成员概览")
        self.context.register_web_api(f"{prefix}/conversions", self._web_conversions, ["GET"], "在途转换")
        logger.info("[羊村基金涨跌榜] 插件页面 Web API 已注册")

    def _web_db_rows(self) -> list[dict]:
        import sqlite3
        con = _db_connect(_db_path())
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        rows = con.execute(
            "SELECT id, member, fund_code, fund_name, principal, current, "
            "last_change, last_update, confirmed, shares, "
            "COALESCE(realized_pnl,0) AS realized_pnl FROM members_funds "
            "ORDER BY member, fund_code"
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def _web_conversions(self) -> list[dict]:
        """在途转换列表（页面展示用）。"""
        try:
            return self._storage.active_conversions()
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 读取在途转换失败: {e}")
            return []

    def _web_summary_rows(self) -> list[dict]:
        import sqlite3
        con = _db_connect(_db_path())
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        rows = con.execute(
            "SELECT member, principal, current, last_change, confirmed, pending_amount, "
            "COALESCE(realized_pnl,0) FROM members_funds"
        ).fetchall()
        con.close()
        agg: dict[str, dict] = {}
        for r in rows:
            m = agg.setdefault(r["member"], {
                "principal": 0.0, "current": 0.0, "chg_w": 0.0, "cnt": 0,
                "pend": 0, "pendamt": 0.0, "pend_principal": 0.0, "realized": 0.0})
            if not r["confirmed"]:
                m["pend"] += 1
                m["pend_principal"] += r["principal"]
                continue
            m["principal"] += r["principal"]
            m["current"] += r["current"]
            m["realized"] += float(r[6] or 0.0)
            m["chg_w"] += r["current"] * (r["last_change"] or 0.0) / 100.0
            m["cnt"] += 1
            pa = r["pending_amount"] or 0.0
            m["pendamt"] += pa
            if pa > 0:
                m["pend"] += 1
        out = []
        for member in sorted(agg):
            m = agg[member]
            pnl = m["current"] - m["principal"] + m["realized"]
            liquidated = m["current"] <= 0.005 and m["principal"] <= 0.005
            day = 0.0 if liquidated else ((m["chg_w"] / m["current"]) if m["current"] else 0.0)
            out.append({
                "member": member,
                "day_chg": round(day * 100, 2),
                "principal": round(m["principal"], 2),
                "current": round(m["current"], 2),
                "pnl": round(pnl, 2),
                "realized": round(m["realized"], 2),
                "float_pnl": round(pnl - m["realized"], 2),
                "today_pnl": 0.0 if liquidated else round(m["chg_w"], 2),
                "liquidated": liquidated,
                "cnt": m["cnt"],
                "pend": m["pend"],
                "pendamt": round(m["pendamt"], 2),
                "pend_principal": round(m["pend_principal"], 2),
            })
        out.sort(key=lambda x: (x["liquidated"], -x["day_chg"]))
        return out

    async def _web_summary(self):
        from astrbot.api.web import json_response
        return json_response(self._web_summary_rows())

    async def _web_records(self):
        from astrbot.api.web import json_response
        return json_response(self._web_db_rows())

    async def _web_add(self):
        from astrbot.api.web import json_response, request
        data = await request.json(default={}) or {}
        member = str(data.get("member") or "").strip()
        code = re.sub(r"\D", "", str(data.get("fund_code") or ""))[:6]
        name = str(data.get("fund_name") or "").strip()
        try:
            principal = float(str(data.get("principal") or "0").replace(",", "").strip())
        except (TypeError, ValueError):
            principal = 0.0
        if not member or not code or principal <= 0:
            return json_response({"ok": False, "error": "成员、基金代码(6位)、本金均需有效"}, status_code=400)
        import sqlite3
        con = _db_connect(_db_path())
        try:
            con.execute(
                "INSERT INTO members_funds (member, fund_code, fund_name, principal, current, "
                "last_change, last_update, confirmed) VALUES (?,?,?,?,?,0,'',1)",
                (member, code, name or code, principal, principal),
            )
            con.commit()
            ok, err = True, ""
        except sqlite3.IntegrityError:
            ok, err = False, f"该成员已持有此基金（{member}/{code}），请改用编辑或先删除"
        finally:
            con.close()
        return json_response({"ok": ok, "error": err})

    async def _web_update(self, rid):
        from astrbot.api.web import json_response, request
        data = await request.json(default={}) or {}
        rid = int(rid)
        import sqlite3
        con = _db_connect(_db_path())
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM members_funds WHERE id=?", (rid,)).fetchone()
        if not row:
            con.close()
            return json_response({"ok": False, "error": "记录不存在"}, status_code=404)
        member = str(data.get("member") or "").strip() or row["member"]
        code = (re.sub(r"\D", "", str(data.get("fund_code") or ""))[:6]) or row["fund_code"]
        name = str(data.get("fund_name") or "").strip()
        new_p = data.get("principal")
        try:
            principal = float(str(new_p).replace(",", "").strip()) if new_p not in (None, "") else row["principal"]
        except (TypeError, ValueError):
            principal = row["principal"]
        if not member or not code or principal <= 0:
            con.close()
            return json_response({"ok": False, "error": "成员、基金代码、本金需有效"}, status_code=400)
        dup = con.execute(
            "SELECT id FROM members_funds WHERE member=? AND fund_code=? AND id<>?",
            (member, code, rid),
        ).fetchone()
        if dup:
            con.close()
            return json_response({"ok": False, "error": f"已存在同成员同基金（{member}/{code}）的记录"}, status_code=400)
        # 现资金规则：本金发生变化时重置为本金；转待确认时资金回到本金；否则保留原现资金
        confirmed_new = data.get("confirmed")
        confirmed = row["confirmed"] if confirmed_new is None else (1 if confirmed_new else 0)
        if principal != row["principal"] or confirmed == 0:
            current = principal
        else:
            current = row["current"]
        con.execute(
            "UPDATE members_funds SET member=?, fund_code=?, fund_name=?, principal=?, "
            "current=?, shares=0, confirmed=?, last_change=0 WHERE id=?",
            (member, code, name or code, principal, current, confirmed, rid),
        )
        con.commit()
        con.close()
        return json_response({"ok": True, "error": ""})

    async def _web_delete(self, rid):
        from astrbot.api.web import json_response
        rid = int(rid)
        import sqlite3
        con = _db_connect(_db_path())
        cur = con.execute("DELETE FROM members_funds WHERE id=?", (rid,))
        con.commit()
        n = cur.rowcount
        con.close()
        if not n:
            return json_response({"ok": False, "error": "记录不存在"}, status_code=404)
        return json_response({"ok": True, "error": ""})

    async def _web_record_state(self, rid):
        from astrbot.api.web import json_response, request
        data = await request.json(default={}) or {}
        rid = int(rid)
        confirmed = 1 if data.get("confirmed") else 0
        import sqlite3
        con = _db_connect(_db_path())
        if confirmed:
            import datetime as _dt
            # 转已确认：清空待确认期间残留的涨跌显示，等每日更新按确认日净值再刷
            cur = con.execute(
                "UPDATE members_funds SET confirmed=1, confirm_date=?, last_change=0 WHERE id=?",
                (_dt.datetime.now().strftime("%Y-%m-%d"), rid))
        else:
            # 转待确认：资金回到本金，不再显示涨跌，确认转正后才按净值更新
            cur = con.execute(
                "UPDATE members_funds SET confirmed=0, current=principal, shares=0, "
                "last_change=0, last_update='', confirm_date='' WHERE id=?", (rid,))
        con.commit()
        n = cur.rowcount
        con.close()
        if not n:
            return json_response({"ok": False, "error": "记录不存在"}, status_code=404)
        return json_response({"ok": True, "error": "", "confirmed": bool(confirmed)})

    async def _web_schedules(self):
        from astrbot.api.web import json_response
        import sqlite3
        con = _db_connect(_db_path())
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        rows = con.execute(
            "SELECT s.member, s.fund_code, s.confirm_at, "
            "(SELECT m.fund_name FROM members_funds m WHERE m.member=s.member AND m.fund_code=s.fund_code) AS fund_name "
            "FROM confirm_schedules s ORDER BY s.confirm_at"
        ).fetchall()
        con.close()
        return json_response([dict(r) for r in rows])

    async def _web_schedule_upsert(self):
        from astrbot.api.web import json_response, request
        data = await request.json(default={}) or {}
        member = str(data.get("member") or "").strip()
        fund_code = str(data.get("fund_code") or "").strip()
        confirm_at = str(data.get("confirm_at") or "").strip()
        if not member or not fund_code or not confirm_at:
            return json_response({"ok": False, "error": "成员、基金代码与确认时间均需填写"}, status_code=400)
        from datetime import datetime
        try:
            datetime.strptime(confirm_at, "%Y-%m-%d %H:%M")
        except ValueError:
            return json_response({"ok": False, "error": "时间格式需为 YYYY-MM-DD HH:MM，如 2026-10-22 15:00"}, status_code=400)
        import sqlite3
        con = _db_connect(_db_path())
        con.execute(
            "INSERT OR REPLACE INTO confirm_schedules(member, fund_code, confirm_at) VALUES(?,?,?)",
            (member, fund_code, confirm_at),
        )
        con.commit()
        con.close()
        return json_response({"ok": True, "error": ""})

    async def _web_schedule_delete(self, member):
        from astrbot.api.web import json_response
        import sqlite3
        con = _db_connect(_db_path())
        cur = con.execute("DELETE FROM confirm_schedules WHERE member=?", (member,))
        con.commit()
        n = cur.rowcount
        con.close()
        if not n:
            return json_response({"ok": False, "error": "该成员没有预约计划"}, status_code=404)
        return json_response({"ok": True, "error": ""})

    async def _web_schedule_delete_one(self, member, fund_code):
        from astrbot.api.web import json_response
        import sqlite3
        con = _db_connect(_db_path())
        cur = con.execute(
            "DELETE FROM confirm_schedules WHERE member=? AND fund_code=?",
            (member, fund_code))
        con.commit()
        n = cur.rowcount
        con.close()
        if not n:
            return json_response({"ok": False, "error": "该条没有预约计划"}, status_code=404)
        return json_response({"ok": True, "error": ""})

    async def _web_members(self):
        from astrbot.api.web import json_response
        import sqlite3
        con = _db_connect(_db_path())
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        rows = con.execute(
            "SELECT member, principal, current, confirmed, pending_amount, "
            "COALESCE(realized_pnl,0) FROM members_funds"
        ).fetchall()
        con.close()
        stats: dict[str, dict] = {}
        for r in rows:
            m = stats.setdefault(r["member"], {"cnt": 0, "principal": 0.0, "current": 0.0,
                                               "pend": 0, "pendamt": 0.0, "pend_principal": 0.0,
                                               "realized": 0.0})
            if not r["confirmed"]:
                m["pend"] += 1
                m["pend_principal"] += r["principal"]
                continue
            m["cnt"] += 1
            m["principal"] += r["principal"]
            m["current"] += r["current"]
            m["realized"] += float(r[5] or 0.0)
            pa = r["pending_amount"] or 0.0
            m["pendamt"] += pa
            if pa > 0:
                m["pend"] += 1  # 买入挂账的份额同样待确认，计入待确认条数
        out = []
        for name in sorted(GROUP_MEMBERS, key=lambda x: x.lower()):
            s = stats.get(name)
            if s is None:
                s = {"cnt": 0, "principal": 0.0, "current": 0.0, "pend": 0,
                     "pendamt": 0.0, "pend_principal": 0.0, "realized": 0.0}
            out.append({
                "member": name,
                "qq": GROUP_MEMBERS[name],
                "cnt": s["cnt"],
                "principal": round(s["principal"], 2),
                "current": round(s["current"], 2),
                "pnl": round(s["current"] - s["principal"] + s["realized"], 2),
                "realized": round(s["realized"], 2),
                "pend": s["pend"],
                "pendamt": round(s["pendamt"], 2),
                "pend_principal": round(s["pend_principal"], 2),
            })
        return json_response(out)

    # ---------- 工具 ----------

    def _is_mention_bot(self, event: AstrMessageEvent) -> bool:
        try:
            self_id = str(event.get_self_id())
        except Exception:
            return False
        if not self_id:
            return False
        for comp in event.get_messages():
            if isinstance(comp, At):
                qq = str(getattr(comp, "qq", ""))
                name = str(getattr(comp, "name", "") or "")
                # QQ 官方有时 qq 字段给的是 openid/空，name 才是「AA全群机器人/机器人」；
                # 只认 self_id 会让「基金持仓」这类指令在 @ 之后被静默吞掉。
                if qq and (qq == self_id or qq == "qq_official"):
                    return True
                if name and ("机器人" in name or "机器人" in name):
                    return True
        text = event.message_str or ""
        if ("@" in text or "at" in text.lower()) and self_id in text:
            return True
        return False

    # ---------- 涨跌榜 ----------

    def _board_live_metrics(self):
        """涨跌榜「今日」口径：优先用盘中实时估算（与盘中估值同源），取不到再退回最近净值日涨跌。
        返回 (member_est: {member:(涨跌%, 盈亏¥)}, fund_est: {code: 涨跌%}, live: bool)"""
        member_est: dict[str, tuple] = {}
        fund_est: dict[str, float] = {}
        live = False
        # 只在「开盘后、每日净值更新前」用盘中估算；盘前/净值更新后一律按官方净值日变动
        _lt = time.localtime()
        if _lt.tm_hour < 9 or _lt.tm_hour >= 20:
            return member_est, fund_est, live
        try:
            rows = self._collect_estimate_rows()
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 涨跌榜实时估算失败，退回净值日涨跌: {e}")
            rows = []
        for row in rows or []:
            for it in row.get("items") or []:
                if it.get("est") is not None:
                    fund_est[it["fund_code"]] = it["est"]
            if row.get("est") is not None:
                member_est[row["member"]] = (row["est"], row.get("amount") or 0.0)
                live = True
        return member_est, fund_est, live

    def _render_board(self) -> str:
        records = self._storage.all_records()
        if not records:
            return "📊 羊村基金涨跌榜\n━━━━━━━━━━━━\n暂无数据。请 @我 发送「导入数据」并附持仓截图录入。"
        # 按成员聚合：已确认计入资金统计，待确认单独显示涨跌、不计入总资金
        agg: dict[str, dict] = {}
        pend_items: dict[str, list] = {}
        for r in records:
            # 已清零的转出基金（转换结算后 principal=0 current=0）不再计入榜；
            # 但清仓结算过的（有 realized_pnl）要留下，否则已实现盈亏就再也看不到了
            if (r["confirmed"] and (r["principal"] or 0) <= 0.005 and (r["current"] or 0) <= 0.005
                    and abs(r.get("realized_pnl") or 0.0) < 0.005):
                continue
            m = agg.setdefault(r["member"], {
                "principal": 0.0, "current": 0.0, "chg_w": 0.0, "cnt": 0,
                "pend": 0, "pendamt": 0.0, "pend_principal": 0.0, "realized": 0.0})
            if not r["confirmed"]:
                m["pend"] += 1
                m["pend_principal"] += r["principal"]
                pend_items.setdefault(r["member"], []).append(r)
                continue
            m["principal"] += r["principal"]
            m["current"] += r["current"]
            m["realized"] += r.get("realized_pnl") or 0.0
            m["chg_w"] += r["current"] * (r["last_change"] or 0.0) / 100.0
            m["cnt"] += 1
            pa = r.get("pending_amount") or 0.0
            m["pendamt"] += pa
            if pa > 0:
                m["pend"] += 1
        lines = ["📊 羊村基金涨跌榜", "━━━━━━━━━━━━"]
        member_est, fund_est, live = self._board_live_metrics()
        # 按日涨幅降序排列（冠军第一），平手按总资金降序
        _board_rows = []
        for member in set(list(agg) + list(pend_items)):
            m = agg.get(member)
            if m:
                liquidated = m["current"] <= 0.005 and m["principal"] <= 0.005
                if liquidated:
                    day_chg = None
                elif member in member_est:
                    day_chg = member_est[member][0] / 100.0
                    m["chg_w"] = member_est[member][1]
                else:
                    day_chg = (m["chg_w"] / m["current"]) if m["current"] else 0.0
            else:
                liquidated = False
                day_chg = -999.0  # 纯待确认排最后
            _board_rows.append((member, m, day_chg, liquidated))
        _board_rows.sort(key=lambda x: (x[3], -(x[2] if x[2] is not None else -999.0),
                                        -(x[1]["current"] if x[1] else 0)))
        for member, m, day_chg, liquidated in _board_rows:
            pend = pend_items.get(member)
            if m:
                # App 口径：待确认买入也计入现资金/本金（两边都加，持有收益不变）
                disp_curr = m["current"] + m["pendamt"]
                disp_prin = m["principal"] + m["pendamt"]
                pnl = disp_curr - disp_prin + m["realized"]
                real = m["realized"]
                chg_txt = "—" if day_chg is None else f"{day_chg * 100:+.2f}%"
                today_txt = ("—" if liquidated
                             else f"今日盈亏 {'+' if m['chg_w']>=0 else '-'}¥{abs(m['chg_w']):,.2f}")
                seg = f"👤 {member}  {chg_txt}  " \
                      f"现资金 ¥{disp_curr:,.2f}  " \
                      f"总盈亏 {'+' if pnl>=0 else '-'}¥{abs(pnl):,.2f}  " \
                      f"{today_txt}"
                pend_parts = []
                if m["pend_principal"]:
                    pend_parts.append(f"待确认¥{m['pend_principal']:,.0f}")
                if m["pendamt"]:
                    pend_parts.append(f"买入待确认¥{m['pendamt']:,.0f}")
                if pend_parts:
                    seg += f"  ·  {'、'.join(pend_parts)}"
                lines.append(seg)
            else:
                lines.append(f"👤 {member}  （份额待确认，暂不计入总资金）")
            if pend:
                for p in pend:
                    pe = fund_est.get(p["fund_code"])
                    chg = (f"{pe:+.2f}%" if pe is not None else
                           (f"{p['last_change']:+.2f}%" if p.get("last_change") is not None else "待更新"))
                    lines.append(f"   ⏳ {p['fund_name'] or p['fund_code']} {p['fund_code']}  {chg}")
        lines.append("━━━━━━━━━━━━")
        lines.append("💡 今日盈亏为盘中实时估算，未开盘时按最近净值日；"
                     "待确认数据到点自动转正，更新后我会在群里提示。" if live else
                     "💡 待确认数据到点自动转正；每日基金变化会自动更新，更新后我会在群里提示。")
        return "\n".join(lines)

    def _find_font(self) -> str | None:
        for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
                  r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc"):
            if os.path.exists(p):
                return p
        return None

    def _render_board_image(self) -> str | None:
        """把涨跌榜渲染成 PNG 图片，返回本地路径；失败或无需图表返回 None。"""
        from PIL import Image, ImageDraw, ImageFont
        font_path = self._find_font()
        if not font_path:
            return None
        # emoji（皇冠👑）用 Segoe UI Emoji，避免微软雅黑里显示为方框
        emoji_path = (r"C:\Windows\Fonts\seguiemj.ttf"
                      if os.path.exists(r"C:\Windows\Fonts\seguiemj.ttf")
                      else font_path)
        records = self._storage.all_records()
        if not records:
            return None
        agg: dict[str, dict] = {}
        pend_funds: dict[str, list] = {}
        for r in records:
            # 已清零的转出基金（转换结算后 principal=0 current=0）不再计入榜；
            # 但清仓结算过的（有 realized_pnl）要留下，否则已实现盈亏就再也看不到了
            if (r["confirmed"] and (r["principal"] or 0) <= 0.005 and (r["current"] or 0) <= 0.005
                    and abs(r.get("realized_pnl") or 0.0) < 0.005):
                continue
            m = agg.setdefault(r["member"], {
                "principal": 0.0, "current": 0.0, "chg_w": 0.0, "cnt": 0,
                "pend": 0, "pendamt": 0.0, "pend_principal": 0.0, "realized": 0.0})
            if not r["confirmed"]:
                # 整条待确认：只计入数量与待确认本金，收进明细，不混入已确认份额的涨跌统计
                m["pend"] += 1
                m["pend_principal"] += r["principal"]
                pend_funds.setdefault(r["member"], []).append(r)
                continue
            m["principal"] += r["principal"]
            m["current"] += r["current"]
            m["realized"] += r.get("realized_pnl") or 0.0
            m["chg_w"] += r["current"] * (r["last_change"] or 0.0) / 100.0
            m["cnt"] += 1
            pa = r.get("pending_amount") or 0.0
            m["pendamt"] += pa
            if pa > 0:
                m["pend"] += 1  # 买入挂账的份额同样待确认，计入待确认条数
        rows = []
        member_est, fund_est, live = self._board_live_metrics()
        for member in set(list(agg) + list(pend_funds)):
            m = agg.get(member)
            if m is None:
                m = {"principal": 0.0, "current": 0.0, "chg_w": 0.0, "cnt": 0,
                     "pend": len(pend_funds.get(member, [])), "pendamt": 0.0,
                     "pend_principal": sum(r["principal"] for r in pend_funds.get(member, []))}
            pnl = m["current"] - m["principal"] + m["realized"]
            liquidated = m["current"] <= 0.005 and m["principal"] <= 0.005
            if liquidated:
                day_chg = None
            elif member in member_est:
                # 盘中实时估算优先：与「盘中估值」同一口径，避免把昨天净值日的涨跌当成今天
                day_chg = member_est[member][0] / 100.0
                m["chg_w"] = member_est[member][1]
            else:
                day_chg = (m["chg_w"] / m["current"]) if m["current"] else -999.0
            rows.append((member, day_chg, m["current"], pnl, m["pend"], m["pendamt"],
                         m["pend_principal"], m["chg_w"], pend_funds.get(member, []),
                         m["realized"], liquidated))
        # 按日涨幅降序排列（冠军第一），平手按总资金降序；清仓成员排最后且不当冠军
        rows.sort(key=lambda r: (r[10], -(r[1] if r[1] is not None else -999.0), -r[2]))
        # 冠军 = 当日涨幅最高的非清仓成员
        champion = None
        for _r in rows:
            if not _r[10]:
                champion = _r[0]
                break

        def vgrad(dr, x0, y0, x1, y1, c1, c2):
            """垂直渐变填充。c1/c2 为 RGB 三元组。"""
            h = y1 - y0
            for i in range(h):
                t = i / max(h - 1, 1)
                r = int(c1[0] + (c2[0] - c1[0]) * t)
                g = int(c1[1] + (c2[1] - c1[1]) * t)
                b = int(c1[2] + (c2[2] - c1[2]) * t)
                dr.line([(x0, y0 + i), (x1, y0 + i)], fill=(r, g, b))

        title_f = ImageFont.truetype(font_path, 48)
        sub_f = ImageFont.truetype(font_path, 22)
        member_f = ImageFont.truetype(font_path, 32)
        num_f = ImageFont.truetype(font_path, 27)
        small_f = ImageFont.truetype(font_path, 22)
        emoji_f = ImageFont.truetype(emoji_path, 44)
        emoji_sm = ImageFont.truetype(emoji_path, 26)
        up = "#D93B34"; down = "#0E9F6E"; flat = "#8A90A0"
        gold = "#F5B301"
        avatar_colors = ["#E8B15E", "#5B9BD5", "#70AD47", "#ED7D31",
                         "#A0522D", "#9467BD", "#2F9E8F", "#C0504D"]

        pad = 44
        title_h = 138
        foot_h = 84
        pend_h = 58  # 每条待确认明细高度
        row_gap = 20
        # 自适应几何：根据每行内容计算宽度与行高（宽随最长内容，高随胶囊数量）
        _tmp = Image.new("RGB", (10, 10)); _td = ImageDraw.Draw(_tmp)
        row_meta = []; W_eff = 0
        for member, day_chg, current, pnl, pend, pendamt, pend_principal, today_pnl, pend_fs, realized, liquidated in rows:
            mb = _td.textbbox((0, 0), member, font=member_f)
            name_w = (mb[2] - mb[0]) + (44 if member == champion else 0)
            caps = []
            if pend:
                t = f"待确认 {pend}"; b = _td.textbbox((0, 0), t, font=small_f); caps.append((t, "#FFF3DF", "#C77700", b[2] - b[0] + 20))
            if pend_principal > 0:
                t = f"待确认 ¥{pend_principal:,.0f}"; b = _td.textbbox((0, 0), t, font=small_f); caps.append((t, "#FFF3DF", "#C77700", b[2] - b[0] + 20))
            if pendamt > 0:
                t = f"买入待确认 ¥{pendamt:,.0f}"; b = _td.textbbox((0, 0), t, font=small_f); caps.append((t, "#E6F1FF", "#2E6BD6", b[2] - b[0] + 20))
            caps_w = sum(c[3] for c in caps) + 12 * max(len(caps) - 1, 0)
            chg_txt = "—" if day_chg is None else f"{day_chg * 100:+.2f}%"
            b2 = _td.textbbox((0, 0), chg_txt, font=num_f); cw2 = b2[2] - b2[0] + 28
            total_txt = f"总盈亏 {'+' if pnl >= 0 else '-'}¥{abs(pnl):,.2f}"; bt = _td.textbbox((0, 0), total_txt, font=num_f)
            today_txt = "—" if liquidated else f"今日盈亏 {'+' if today_pnl >= 0 else '-'}¥{abs(today_pnl):,.2f}"; bd = _td.textbbox((0, 0), today_txt, font=small_f)
            has_real = False
            right_w = max(cw2, bt[2] - bt[0], bd[2] - bd[0])
            has_caps = bool(caps)
            left_content = max(name_w, caps_w) if has_caps else name_w
            row_h = (180 if has_caps else 134)
            W_eff = max(W_eff, left_content + 32 + right_w)
            row_meta.append(dict(caps=caps, has_caps=has_caps, row_h=row_h,
                                 data_y=(120 if has_caps else 66),
                                 today_y=(150 if has_caps else 96),
                                 caps_y=(70 if has_caps else None)))
        W = int(max(W_eff, 600)) + 2 * pad
        H = title_h + foot_h
        for _row, rm in zip(rows, row_meta):
            H += rm["row_h"] + row_gap
            if _row[8]:
                H += pend_h * len(_row[8]) + 12

        # 背景：浅蓝渐变
        img = Image.new("RGB", (W, H), "#FFFFFF")
        d = ImageDraw.Draw(img)
        vgrad(d, 0, 0, W, H, (240, 244, 255), (252, 253, 255))

        # 标题栏：深蓝→靛紫渐变 + 底部圆角
        bar = Image.new("RGBA", (W, title_h))
        db = ImageDraw.Draw(bar)
        vgrad(db, 0, 0, W, title_h, (26, 38, 96), (91, 71, 168))
        bmask = Image.new("L", (W, title_h), 0)
        ImageDraw.Draw(bmask).rounded_rectangle(
            [0, 0, W - 1, title_h - 1], radius=22,
            corners=(False, False, True, True), fill=255)
        bar.putalpha(bmask)
        img.paste(bar, (0, 0), bar)
        d.text((pad, 34), "羊村基金涨跌榜", font=title_f, fill="#FFFFFF")
        d.text((pad, 96), "更新于 " + time.strftime("%Y-%m-%d %H:%M") +
               f" · 每日 {self.cfg.get('daily_update_time', '20:00')} 自动更新",
               font=sub_f, fill="#D9DFF6")
        d.text((W - pad - 64, 40), "🐑", font=emoji_f, fill="#F5B301")

        # 数据行
        y = title_h
        avatar_d = 58
        for (member, day_chg, current, pnl, pend, pendamt, pend_principal, today_pnl, pend_fs, realized, liquidated), rm in zip(rows, row_meta):
            row_h = rm["row_h"]
            color = flat if day_chg is None else (up if day_chg > 0 else (down if day_chg < 0 else flat))
            pnl_color = up if pnl >= 0 else down
            # 卡片投影（浅灰偏移）+ 本体
            d.rounded_rectangle([pad + 2, y + 6, W - pad + 2, y + row_h + 6],
                                radius=16, fill="#E4E9F6")
            d.rounded_rectangle([pad, y, W - pad, y + row_h],
                                radius=16, fill="#FFFFFF",
                                outline="#E3E8F6", width=1)
            # 头像：白描边环 + 真实头像（固定上排，行高随胶囊数量自适应）
            ax = pad + 16
            ay = y + 6
            avatar = self._load_avatar(member, avatar_d)
            if avatar is not None:
                d.ellipse([ax - 5, ay - 5, ax + avatar_d + 5, ay + avatar_d + 5],
                          fill="#FFFFFF", outline="#E3E8F6", width=2)
                img.paste(avatar, (int(ax), int(ay)), avatar)
            else:
                d.ellipse([ax, ay, ax + avatar_d, ay + avatar_d],
                          fill=avatar_colors[hash(member) % len(avatar_colors)])
                first = member[0] if member else "?"
                bb = d.textbbox((0, 0), first, font=member_f)
                d.text((ax + avatar_d / 2 - (bb[2] - bb[0]) / 2,
                        ay + avatar_d / 2 - (bb[3] - bb[1]) / 2 - 2),
                       first, font=member_f, fill="#FFFFFF")
            # 名字（冠军名字前加👑）
            nx = pad + 16 + avatar_d + 18
            if member == champion:
                d.text((nx, y + 12), "👑", font=emoji_f, fill=gold)
                nx += 44
            d.text((nx, y + 14), member, font=member_f, fill="#20242E")
            # 待确认胶囊 + 买入待确认挂账胶囊（自适应单行，行高已按胶囊数量撑开）
            if rm["has_caps"]:
                cy = y + rm["caps_y"]; cx = pad + 16
                for _ci, (label, fillc, txtc, cw) in enumerate(rm["caps"]):
                    if _ci > 0:
                        cx += 12
                    chh = 36
                    d.rounded_rectangle([cx, cy, cx + cw, cy + chh], radius=chh / 2, fill=fillc)
                    b = d.textbbox((0, 0), label, font=small_f)
                    d.text((cx + 10, cy + (chh - (b[3] - b[1])) / 2 - 1), label, font=small_f, fill=txtc)
                    cx += cw
            # 现资金：App 口径含待确认买入挂账（本金同理，持有收益不受影响）
            d.text((nx, y + rm["data_y"]), f"现资金  ¥{current + pendamt:,.2f}", font=num_f, fill="#5B6270")
            # 涨幅胶囊（右上）
            # 涨幅胶囊（右上）
            chg_txt = "—" if day_chg is None else f"{day_chg * 100:+.2f}%"
            cbg = ("#ECEDF2", flat) if day_chg is None else (
                ("#FCE9E7", up) if day_chg > 0 else (
                ("#E3F4EC", down) if day_chg < 0 else ("#ECEDF2", flat)))
            tw2 = d.textbbox((0, 0), chg_txt, font=num_f)
            cw2 = tw2[2] - tw2[0] + 28
            chh2 = 40
            cx2 = W - pad - 14 - cw2
            cy2 = y + 12
            d.rounded_rectangle([cx2, cy2, cx2 + cw2, cy2 + chh2], radius=chh2 / 2,
                                fill=cbg[0])
            d.text((cx2 + 14, cy2 + (chh2 - (tw2[3] - tw2[1])) / 2 - 1),
                   chg_txt, font=num_f, fill=cbg[1])
            # 总盈亏（右下）
            total_txt = f"总盈亏 {'+' if pnl >= 0 else '-'}¥{abs(pnl):,.2f}"
            d.text((W - pad - 14, y + rm["data_y"]), total_txt, font=num_f,
                   fill=pnl_color, anchor="ra")
            # 今日盈亏（右下第二行）
            today_txt = "—" if liquidated else f"今日盈亏 {'+' if today_pnl >= 0 else '-'}¥{abs(today_pnl):,.2f}"
            today_color = flat if liquidated else (up if today_pnl > 0 else (down if today_pnl < 0 else flat))
            d.text((W - pad - 14, y + rm["today_y"]), today_txt, font=small_f,
                   fill=today_color, anchor="ra")
            y += rm["row_h"] + row_gap
            # 待确认基金明细：显示最新涨跌，不计入成员总资金；自适应截断 + 宽松行距
            if pend_fs:
                def _ellipsize(text, font, max_w):
                    if max_w <= 0:
                        return ""
                    b = d.textbbox((0, 0), text, font=font)
                    if b[2] - b[0] <= max_w:
                        return text
                    t = text
                    while t:
                        eb = d.textbbox((0, 0), t + "…", font=font)
                        if eb[2] - eb[0] <= max_w:
                            break
                        t = t[:-1]
                    return t + "…"

                ph = pend_h * len(pend_fs) + 20
                d.rounded_rectangle([pad + 2, y + 4, W - pad + 2, y + ph - 4],
                                    radius=14, fill="#FFF9EF", outline="#F0E2C6", width=1)
                py = y + 14
                for p in pend_fs:
                    pe = fund_est.get(p["fund_code"])
                    _pv = pe if pe is not None else p.get("last_change")
                    pchg = f"{_pv:+.2f}%" if _pv is not None else "待更新"
                    pcolor = up if (_pv or 0) > 0 else (
                        down if (_pv or 0) < 0 else flat)
                    # 涨跌胶囊（右上）
                    tw = d.textbbox((0, 0), pchg, font=small_f)
                    cw = tw[2] - tw[0] + 26; chh = 34
                    cx = W - pad - 14 - cw
                    cy = py + 2
                    cbg = "#FCE9E7" if pcolor == up else (
                        "#E3F4EC" if pcolor == down else "#ECEDF2")
                    d.rounded_rectangle([cx, cy, cx + cw, cy + chh], radius=chh / 2, fill=cbg)
                    d.text((cx + 13, cy + (chh - (tw[3] - tw[1])) / 2 - 1), pchg,
                           font=small_f, fill=pcolor)
                    # 基金名（自适应截断，不顶到涨跌胶囊）
                    name_max = cx - 14 - (pad + 16)
                    pname = _ellipsize(p["fund_name"] or p["fund_code"], small_f, name_max)
                    d.text((pad + 16, py), pname, font=small_f, fill="#20242E")
                    # 代码 + 待确认标签（第二行，行距宽松）
                    d.text((pad + 16, py + 32), p["fund_code"], font=small_f, fill="#9AA0B0")
                    ttb = d.textbbox((0, 0), "⏳ 待确认", font=small_f)
                    d.text((pad + 18 + (ttb[2] - ttb[0]) + 8, py + 32), "⏳ 待确认",
                           font=small_f, fill="#C77700")
                    py += pend_h
                y += ph + 8

        # 底部
        d.line([(pad, H - foot_h + 12), (W - pad, H - foot_h + 12)],
               fill="#DDE3F2", width=1)
        d.text((pad, H - foot_h + 26),
               ("今日涨跌＝盘中实时估算 · 待确认到点自动转正 · 红涨绿跌" if live
                else "涨跌＝最近净值日变动 · 待确认到点自动转正 · 红涨绿跌"),
               font=small_f, fill="#9AA0B0")
        out = os.path.join(str(_db_path().parent), "board_tmp.png")
        img.save(out, "PNG")
        return out

    def _render_update_image(self, groups: list[dict]) -> str | None:
        """把『何时更新』联网分析结果渲染成 PNG，按成员分组。"""
        from PIL import Image, ImageDraw, ImageFont
        font_path = self._find_font()
        if not font_path:
            return None
        emoji_path = (r"C:\Windows\Fonts\seguiemj.ttf"
                      if os.path.exists(r"C:\Windows\Fonts\seguiemj.ttf") else font_path)

        def vgrad(dr, x0, y0, x1, y1, c1, c2):
            h = y1 - y0
            for i in range(h):
                t = i / max(h - 1, 1)
                dr.line([(x0, y0 + i), (x1, y0 + i)], fill=(
                    int(c1[0] + (c2[0] - c1[0]) * t),
                    int(c1[1] + (c2[1] - c1[1]) * t),
                    int(c1[2] + (c2[2] - c1[2]) * t)))

        title_f = ImageFont.truetype(font_path, 46)
        sub_f = ImageFont.truetype(font_path, 21)
        member_f = ImageFont.truetype(font_path, 34)
        fund_f = ImageFont.truetype(font_path, 24)
        small_f = ImageFont.truetype(font_path, 21)
        emoji_f = ImageFont.truetype(emoji_path, 42)
        up = "#D93B34"; down = "#0E9F6E"; flat = "#8A90A0"
        W, pad = 960, 56
        title_h = 140; foot_h = 76
        member_head_h = 68; fund_card_h = 108; gap_fund = 16; gap_member = 30
        today = datetime.now().strftime("%Y-%m-%d")
        H = title_h + foot_h
        for g in groups:
            H += member_head_h + len(g["funds"]) * fund_card_h \
                + max(len(g["funds"]) - 1, 0) * gap_fund + gap_member
        img = Image.new("RGB", (W, H), "#FFFFFF"); d = ImageDraw.Draw(img)
        vgrad(d, 0, 0, W, H, (240, 244, 255), (252, 253, 255))
        bar = Image.new("RGBA", (W, title_h)); db = ImageDraw.Draw(bar)
        vgrad(db, 0, 0, W, title_h, (26, 38, 96), (91, 71, 168))
        bmask = Image.new("L", (W, title_h), 0)
        ImageDraw.Draw(bmask).rounded_rectangle(
            [0, 0, W - 1, title_h - 1], radius=22,
            corners=(False, False, True, True), fill=255)
        bar.putalpha(bmask); img.paste(bar, (0, 0), bar)
        d.text((pad, 30), "基金更新状态", font=title_f, fill="#FFFFFF")
        d.text((pad, 86), "更新于 " + time.strftime("%Y-%m-%d %H:%M") + " · 联网天天基金实时净值",
               font=sub_f, fill="#D9DFF6")
        d.text((W - pad - 64, 30), "🕐", font=emoji_f, fill="#F5B301")

        y = title_h
        avatar_d = 56
        for g in groups:
            member = g["member"]
            funds = g["funds"]
            gheight = len(funds) * fund_card_h + max(len(funds) - 1, 0) * gap_fund
            ax = pad + 8; ay = y + member_head_h / 2 - avatar_d / 2
            avatar = self._load_avatar(member, avatar_d)
            if avatar is not None:
                d.ellipse([ax - 4, ay - 4, ax + avatar_d + 4, ay + avatar_d + 4],
                          fill="#FFFFFF", outline="#E3E8F6", width=2)
                img.paste(avatar, (int(ax), int(ay)), avatar)
            else:
                d.ellipse([ax, ay, ax + avatar_d, ay + avatar_d], fill="#5B9BD5")
                first = member[0] if member else "?"
                bb = d.textbbox((0, 0), first, font=member_f)
                d.text((ax + avatar_d / 2 - (bb[2] - bb[0]) / 2,
                        ay + avatar_d / 2 - (bb[3] - bb[1]) / 2 - 2),
                       first, font=member_f, fill="#FFFFFF")
            d.text((ax + avatar_d + 14, y + 6), member, font=member_f, fill="#20242E")
            pend = sum(1 for f in funds if not f["confirmed"])
            sub = f"{len(funds)} 只持仓" + (f" · 待确认 {pend}" if pend else "")
            d.text((ax + avatar_d + 16, y + 42), sub, font=small_f, fill="#8A90A0")
            gy = y + member_head_h
            d.rounded_rectangle([pad, gy, W - pad, gy + gheight], radius=18,
                                fill="#F5F7FC", outline="#E3E8F6", width=1)
            fy = gy + 18
            for f in funds:
                name = f["fund_name"] or f["fund_code"]
                if f["nav_date"] == today:
                    st, sc, sbg = "今日已更新", down, "#E3F4EC"
                elif f["nav_date"]:
                    st, sc, sbg = f"最新 {f['nav_date']}", flat, "#ECEDF2"
                else:
                    st, sc, sbg = "联网失败", "#C0504D", "#FCE9E7"
                chg = f"{f['change']:+.2f}%" if f["change"] is not None else ""
                cchg = up if (f["change"] or 0) > 0 else (down if (f["change"] or 0) < 0 else flat)
                d.text((pad + 26, fy), name, font=fund_f, fill="#20242E")
                d.text((pad + 26, fy + 46), f["fund_code"], font=small_f, fill="#9AA0B0")
                tw = d.textbbox((0, 0), st, font=small_f); cw = tw[2] - tw[0] + 22; chh = 28
                cx = W - pad - 14 - cw; cy = fy + 6
                d.rounded_rectangle([cx, cy, cx + cw, cy + chh], radius=chh / 2, fill=sbg)
                d.text((cx + 11, cy + (chh - (tw[3] - tw[1])) / 2 - 1), st, font=small_f, fill=sc)
                if chg:
                    tw2 = d.textbbox((0, 0), chg, font=fund_f); cw2 = tw2[2] - tw2[0] + 22; chh2 = 32
                    cx2 = cx - 12 - cw2; cy2 = fy + 4
                    cbg = "#FCE9E7" if cchg == up else ("#E3F4EC" if cchg == down else "#ECEDF2")
                    d.rounded_rectangle([cx2, cy2, cx2 + cw2, cy2 + chh2], radius=chh2 / 2, fill=cbg)
                    d.text((cx2 + 11, cy2 + (chh2 - (tw2[3] - tw2[1])) / 2 - 1), chg,
                           font=fund_f, fill=cchg)
                stat = "待确认" if not f["confirmed"] else "已确认"
                statc = "#C77700" if not f["confirmed"] else down
                rule = f["rule"] or _fund_confirm_rule(name)
                d.text((pad + 26, fy + 82), f"{stat} · {rule}", font=small_f, fill=statc)
                fy += fund_card_h + gap_fund
            y += member_head_h + gheight + gap_member

        d.line([(pad, H - foot_h + 10), (W - pad, H - foot_h + 10)], fill="#DDE3F2", width=1)
        d.text((pad, H - foot_h + 24), "净值一般 20:00 后发布当日净值 · 确认份额按 T+N 交易日到账",
               font=small_f, fill="#9AA0B0")
        out = os.path.join(str(_db_path().parent), "update_tmp.png")
        img.save(out, "PNG")
        return out

    def _render_help_image(self) -> str | None:
        """把『使用说明』渲染成 PNG 图片。"""
        from PIL import Image, ImageDraw, ImageFont
        font_path = self._find_font()
        if not font_path:
            return None
        emoji_path = (r"C:\Windows\Fonts\seguiemj.ttf"
                      if os.path.exists(r"C:\Windows\Fonts\seguiemj.ttf") else font_path)

        def vgrad(dr, x0, y0, x1, y1, c1, c2):
            h = y1 - y0
            for i in range(h):
                t = i / max(h - 1, 1)
                dr.line([(x0, y0 + i), (x1, y0 + i)], fill=(
                    int(c1[0] + (c2[0] - c1[0]) * t),
                    int(c1[1] + (c2[1] - c1[1]) * t),
                    int(c1[2] + (c2[2] - c1[2]) * t)))

        sections = [
            ("① 查看涨跌榜", ["@机器人 说「涨跌榜」",
                          "→ 输出成员理财数据：用户 + 涨跌 + 现资金 + 盈亏"]),
            ("② 导入数据", ["@机器人 说「导入数据」并附持仓截图",
                          "→ 自动识别录入成员基金，日期自动取当天",
                          "· 份额不明确 → 暂存【待确认】不参与更新",
                          "· 可回复金额 / 预约确认时间，到点自动转正"]),
            ("③ 查某人的基金", ["@张三 基金情况",
                            "→ 简洁列出张三的基金及当前资金、涨跌、盈亏"]),
            ("④ 删除 / 修改数据", ["@机器人 说「删除基金」→ 回复编号即删除",
                              "@机器人 说「修改数据」→ 「编号 新金额」改本金"]),
            ("⑤ 买入基金", ["@机器人 说「买入基金」/「加仓」→ 编号 + 金额追加本金",
                          "也可直接输入新基金名 + 金额",
                          "· 买入先入账【待确认】，可预约份额确认时间"]),
            ("⑥ 卖出基金 / 交易明细", ["@机器人 说「卖出基金」/「赎回」/「减仓」→ 编号 + 金额（或「全部」）",
                                "只卖已确认本金，待确认份额不受影响",
                                "· 直接发买卖交易截图，自动识别出方案，回「确认」入账",
                                "「交易明细」查看买入/卖出留痕"]),
            ("⑦ 每日自动更新", [f"每天 {self.cfg.get('daily_update_time', '20:00')} "
                             "自动按天天基金最新净值更新成员资金",
                             "到预约时间的待确认数据自动转正更新"]),
            ("⑧ 何时更新（联网分析）", ["@机器人 说「何时更新」→ 联网分析当日净值是否发布、确认规则",
                              "@张三 何时更新 → 只看张三持仓"]),
            ("⑨ 基金转换", ["@机器人 说「转换基金」→ 选转出(「编号 全部」) → 转入基金名 → 转出日 → 确认时间",
                          "· 转出日前源基金继续算涨跌，转出日晚自动按净值结算挂账",
                          "· 历史盈亏完整结转；中途可「取消转换」"]),
            ("⑩ 盘中估值", ["@机器人 说「盘中估值」→ 出图显示你全部基金当前盘中涨跌 + 持仓加权总预估",
                          "· 主动/混合基金用重仓股实时行情估算；指数基金按对应场内ETF行情；仅债券型标 —"]),
        ]
        title_f = ImageFont.truetype(font_path, 46)
        sub_f = ImageFont.truetype(font_path, 21)
        head_f = ImageFont.truetype(font_path, 28)
        txt_f = ImageFont.truetype(font_path, 22)
        emoji_f = ImageFont.truetype(emoji_path, 42)
        W, pad = 960, 56
        title_h = 140; foot_h = 86
        card_h = 70; line_h = 42; card_gap = 30
        H = title_h + foot_h + 12
        for _t, lines in sections:
            H += card_h + len(lines) * line_h + card_gap
        img = Image.new("RGB", (W, H), "#FFFFFF"); d = ImageDraw.Draw(img)
        vgrad(d, 0, 0, W, H, (240, 244, 255), (252, 253, 255))
        bar = Image.new("RGBA", (W, title_h)); db = ImageDraw.Draw(bar)
        vgrad(db, 0, 0, W, title_h, (26, 38, 96), (91, 71, 168))
        bmask = Image.new("L", (W, title_h), 0)
        ImageDraw.Draw(bmask).rounded_rectangle(
            [0, 0, W - 1, title_h - 1], radius=22,
            corners=(False, False, True, True), fill=255)
        bar.putalpha(bmask); img.paste(bar, (0, 0), bar)
        d.text((pad, 30), "羊村基金涨跌榜 · 使用说明", font=title_f, fill="#FFFFFF")
        d.text((pad, 88), "回复关键词即可使用，提示消息会按时自动撤回避免刷屏",
               font=sub_f, fill="#D9DFF6")
        d.text((W - pad - 64, 30), "🐑", font=emoji_f, fill="#F5B301")
        y = title_h
        accent = ["#5B9BD5", "#ED7D31", "#70AD47", "#C0504D", "#9467BD", "#2F9E8F"]
        for idx, (_t, lines) in enumerate(sections):
            ch = card_h + len(lines) * line_h
            d.rounded_rectangle([pad, y, W - pad, y + ch], radius=16,
                                fill="#FFFFFF", outline="#E3E8F6", width=1)
            d.rounded_rectangle([pad, y, pad + 6, y + ch], radius=3, fill=accent[idx % len(accent)])
            d.text((pad + 24, y + 14), _t, font=head_f, fill="#20242E")
            ty = y + card_h
            for ln in lines:
                d.text((pad + 30, ty), ln, font=txt_f, fill="#5B6270")
                ty += line_h
            y += ch + card_gap
        d.line([(pad, H - foot_h + 12), (W - pad, H - foot_h + 12)], fill="#DDE3F2", width=1)
        d.text((pad, H - foot_h + 26), "💡 建议截图包含基金代码，便于自动更新涨跌 · 数据保存在本地 SQLite",
               font=txt_f, fill="#9AA0B0")
        out = os.path.join(str(_db_path().parent), "help_tmp.png")
        img.save(out, "PNG")
        return out

    # ---------- 基金重仓股实时涨跌 ----------
    def _fetch_fund_holdings(self, code: str):
        """拉基金重仓前10股票（带 6 小时缓存，重仓股按季报变，没必要每次推送都抓）。
        返回 (fund_name, [(ticker, name, weight%),...])。"""
        hit = _HOLDINGS_CACHE.get(code)
        if hit and time.time() - hit[0] < HOLDINGS_TTL:
            return hit[1], list(hit[2])
        import urllib.request, re
        def _get(url):
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                       "Referer": "https://fundf10.eastmoney.com/"})
            return urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
        txt = _get(f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={code}&topline=10&year=&month=&rt=0.1")
        m = re.search(r"title='([^']+)'", txt)
        fname = m.group(1) if m else code
        holds = []
        for r in re.findall(r"<tr>(.*?)</tr>", txt, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
            if len(tds) >= 7:
                cells = [re.sub(r"<[^>]+>", "", t).strip() for t in tds]
                if len(cells) >= 7 and "%" in cells[6]:
                    try:
                        w = float(cells[6].replace("%", ""))
                    except ValueError:
                        w = 0.0
                    holds.append((cells[1], cells[2], w))
            if len(holds) >= 10:
                break
        _HOLDINGS_CACHE[code] = (time.time(), fname, list(holds))
        return fname, holds

    def _tx_symbol(self, ticker: str) -> str:
        """把天天基金重仓代码转成腾讯行情代码：A股 sh/sz/bj，港股 hk，美股 us。
        注意：港股代码是 5 位数字（01801/00700），不能按 A 股规则拼成 sz01801，
        否则行情取不到，界面会显示成假的 0.00%。"""
        t = (ticker or "").strip().upper()
        if t.isdigit():
            if len(t) == 5:
                return "hk" + t
            if len(t) == 6:
                if t[0] in "695":
                    return "sh" + t
                if t[0] in "48":
                    return "bj" + t
                return "sz" + t
            return "hk" + t.zfill(5)
        if t.isalpha() and len(t) <= 5:
            return "us" + t
        return "hk" + t.zfill(5)

    def _fetch_us_indices(self) -> dict:
        """美股三大指数昨夜涨跌：返回 {'纳斯达克': (pct, px), '道琼斯': ..., '标普500': ...}。"""
        syms = {"纳指": "us.IXIC", "道指": "us.DJI", "标普": "us.INX"}
        out = {}
        try:
            q = ",".join(syms.values())
            req = urllib.request.Request(f"https://qt.gtimg.cn/q={q}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")
            for line in raw.split(";"):
                if '="' not in line:
                    continue
                key = line.split("=")[0].strip().replace("v_", "")
                f = line.split('"')[1].split("~")
                name = f[1] if len(f) > 1 else key
                try:
                    pct = float(f[32])
                    px = float(f[3])
                except Exception:
                    continue
                short = "纳斯达克" if "纳斯达克" in name else ("道琼斯" if "道琼斯" in name else "标普500")
                out[short] = (pct, px)
        except Exception:
            return {}
        return out

    @staticmethod
    def _is_overseas_fund(name: str) -> bool:
        """海外/美股类基金：QDII、海外、纳斯达克/美国/全球。"""
        n = name or ""
        return any(k in n for k in ("QDII", "海外", "纳斯达克", "美国", "全球"))

    def _overnight_est(self, code: str, name: str, kind: str, idx: dict) -> float | None:
        """海外基金的「昨夜」涨跌：美股刚收盘那一场。
        指数型 QDII 直接取对应指数；主动型 QDII 取重仓股昨夜涨幅加权（腾讯 f[32]
        对美股正是刚收盘那一场的涨跌）。这样白天不会因为基准价重置而显示 0.00。"""
        if kind == "etf":
            if "纳斯达克" in (name or "") and idx.get("纳斯达克"):
                return idx["纳斯达克"][0]
            if "标普500" in (name or "") and idx.get("标普500"):
                return idx["标普500"][0]
            return None
        try:
            _fname, holds = self._fetch_fund_holdings(code)
            if not holds:
                return None
            q = self._fetch_stock_quotes([h[0] for h in holds])
            num = den = 0.0
            for tk, _nm, w in holds:
                chg = (q.get(tk) or (None, None))[0]
                if chg is None:
                    continue
                num += w * chg
                den += w
            return round(num / den, 2) if den else None
        except Exception:
            return None

    def _build_overseas_rows(self, rows: list[dict]) -> list[dict]:
        """从全员估值行里筛出海外持仓，涨跌一律用「昨夜美股这一场」的口径。"""
        idx = self._fetch_us_indices()
        out = []
        for r in rows:
            items = []
            for it in r.get("items") or []:
                nm = it.get("fund_name") or ""
                if not self._is_overseas_fund(nm):
                    continue
                est = self._overnight_est(it.get("fund_code"), nm, it.get("kind") or "none", idx)
                if est is None:
                    est = it.get("est")
                items.append({"fund_code": it.get("fund_code"), "fund_name": nm,
                              "current": it.get("current") or 0.0, "est": est,
                              "kind": it.get("kind") or "none",
                              "in_sell": bool(it.get("in_sell"))})
            if not items:
                continue
            cur = sum(i["current"] or 0.0 for i in items)
            num = den = 0.0
            for i in items:
                if i["est"] is not None and (i["current"] or 0) > 0:
                    num += i["current"] * i["est"]
                    den += i["current"]
            out.append({"member": r["member"], "current": cur,
                        "est": round(num / den, 2) if den else None,
                        "amount": round(num / 100, 2) if den else 0.0,
                        "overnight_amount": round(num / 100, 2) if den else 0.0,
                        "items": items,
                        "pending_amount": r.get("pending_amount") or 0.0})
        return out

    def _fetch_a50_change(self) -> tuple[float, float, str] | None:
        """富时中国A50期货（新浪 hf_CHA50CFD，需带 Referer）：返回 (涨跌幅%, 最新价, 行情时间)。
        A50 09:00 开盘（夜盘 17:00-04:45），A股 09:30 才连续竞价，盘前用它做方向参考。"""
        try:
            req = urllib.request.Request(
                "https://hq.sinajs.cn/list=hf_CHA50CFD",
                headers={"User-Agent": "Mozilla/5.0",
                         "Referer": "https://finance.sina.com.cn/"})
            raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")
            body = raw.split('"')[1] if '"' in raw else ""
            f = body.split(",")
            if len(f) < 10:
                return None
            px = float(f[0]) if f[0] else 0.0
            prev = float(f[7]) if f[7] else 0.0   # 昨收
            if px <= 0 or prev <= 0:
                return None
            return (round((px / prev - 1) * 100, 2), px, f[6] or "")
        except Exception:
            return None

    def _fetch_stock_quotes(self, tickers: list) -> dict:
        """腾讯 qt.gtimg.cn 批量取 (当日涨跌幅%, 现价)，返回 {ticker: (change, price)}。
        现价用于美股「每日收盘重置基线」：白天美股休市时涨跌幅是昨夜那一场的数，
        直接拿去当今日估算会把隔夜涨幅重复算进当天。"""
        if not tickers:
            return {}
        syms = ",".join(self._tx_symbol(t) for t in tickers)
        url = f"https://qt.gtimg.cn/q={syms}"
        for _ in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=12).read().decode("gbk", "ignore")
                out = {}
                for line in raw.strip().split(";"):
                    line = line.strip()
                    if not line or "=" not in line or "none_match" in line:
                        continue
                    var, val = line.split("=", 1)
                    sym = var.replace("v_", "").strip()
                    tk = re.sub(r"^(us|sh|sz|bj|hk)", "", sym)
                    f = val.strip('"').split("~")
                    if len(f) <= 32:
                        continue
                    chg = None
                    if f[32] not in ("", "-"):
                        try:
                            chg = round(float(f[32]), 2)
                        except (ValueError, TypeError):
                            chg = None
                    px = None
                    if f[3] not in ("", "-"):
                        try:
                            px = float(f[3])
                        except (ValueError, TypeError):
                            px = None
                    if chg is not None or px:
                        out[tk] = (chg, px)
                return out
            except Exception:
                time.sleep(0.5)
        return {}

    def _fetch_stock_changes(self, tickers: list) -> dict:
        """腾讯 qt.gtimg.cn 批量取实时涨跌幅%，返回 {ticker: change%}。
        取不到的代码不进结果（界面显示「—」），绝不用 0.0 冒充真实涨跌。"""
        import urllib.request, json
        if not tickers:
            return {}
        syms = ",".join(self._tx_symbol(t) for t in tickers)
        url = f"https://qt.gtimg.cn/q={syms}"
        for _ in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=12).read().decode("gbk", "ignore")
                out = {}
                for line in raw.strip().split(";"):
                    line = line.strip()
                    if not line or "=" not in line or "none_match" in line:
                        continue
                    var, val = line.split("=", 1)
                    sym = var.replace("v_", "").strip()
                    tk = re.sub(r"^(us|sh|sz|bj|hk)", "", sym)
                    f = val.strip('"').split("~")
                    if len(f) > 32 and f[32] not in ("", "-"):
                        try:
                            out[tk] = round(float(f[32]), 2)
                        except (ValueError, TypeError):
                            continue
                return out
            except Exception:
                time.sleep(0.5)
        return {}

    # ---------- 场内 ETF 行情多源（腾讯主 / 东财备 / 新浪兜底） ----------
    def _em_secid(self, ticker: str) -> str:
        """东财 secid：上交所(sh)→1.，深交所(sz)→0.（沪深ETF/股票）。港股/美股无则空。"""
        p = self._tx_symbol((ticker or "").strip())
        if p.startswith("sh"):
            return "1." + ticker.strip()
        if p.startswith("sz"):
            return "0." + ticker.strip()
        return ""

    def _fetch_em_changes(self, codes: list) -> dict:
        """东方财富 ulist 批量取 ETF/股票实时涨跌幅%（f3 为 ×100 放大值，f12 为代码）。
        返回 {code: change%}；失败/取不到不进结果，绝不用 0.0 冒充真实涨跌。"""
        import urllib.request, json
        codes = [c for c in codes if c]
        if not codes:
            return {}
        secids = ",".join(s for s in (self._em_secid(c) for c in codes) if s)
        if not secids:
            return {}
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=%s"
               "&fields=f2,f3,f12,f14" % secids)
        for _ in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore"))
                diff = (data.get("data") or {}).get("diff") or []
                if isinstance(diff, dict):
                    diff = [diff]
                out = {}
                for it in diff:
                    c = str(it.get("f12") or "").strip()
                    p = it.get("f3")
                    if c and p not in (None, "-", ""):
                        try:
                            out[c] = round(float(p) / 100.0, 2)
                        except (ValueError, TypeError):
                            continue
                return out
            except Exception:
                time.sleep(0.5)
        return {}

    def _fetch_sina_changes(self, codes: list) -> dict:
        """新浪 hq.sinajs.cn 批量取 ETF/股票实时涨跌%（需 Referer finance.sina.com.cn）。
        返回 {code: change%}；失败不进结果，绝不用 0.0 冒充真实涨跌。"""
        import urllib.request, re
        codes = [c for c in codes if c]
        if not codes:
            return {}
        syms = ",".join(self._tx_symbol(c) for c in codes)
        url = "https://hq.sinajs.cn/list=%s" % syms
        for _ in range(3):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
                raw = urllib.request.urlopen(req, timeout=12).read().decode("gbk", "ignore")
                out = {}
                for m in re.finditer(r'hq_str_([a-z0-9]+)="([^"]*)"', raw):
                    sym, body = m.group(1), m.group(2)
                    if not body:
                        continue
                    v = body.split(",")
                    if len(v) >= 4 and v[2]:
                        try:
                            price = float(v[3]); prev = float(v[2])
                            if prev:
                                code = re.sub(r"^(us|sh|sz|bj|hk)", "", sym)
                                out[code] = round((price - prev) / prev * 100, 2)
                        except (ValueError, TypeError):
                            continue
                return out
            except Exception:
                time.sleep(0.5)
        return {}

    def _fetch_etf_changes(self, codes: list) -> dict:
        """多源获取场内 ETF 实时涨跌%：腾讯主 → 东财备 → 新浪兜底。
        主源(腾讯)成功后用东财交叉校验，差异>0.3pp 记 warning（不中断）。
        返回 {code: change%}；全部源失败返回 {}（绝不填 0.0 冒充）。"""
        codes = [c for c in codes if c]
        if not codes:
            return {}
        out = self._fetch_stock_changes(codes)
        src = "腾讯" if out else None
        if not out:
            out = self._fetch_em_changes(codes)
            src = "东财" if out else None
        if not out:
            out = self._fetch_sina_changes(codes)
            src = "新浪" if out else None
        if not out:
            logger.warning(f"[羊村基金涨跌榜] ETF行情全部源失败 codes={codes}")
            return {}
        logger.debug(f"[羊村基金涨跌榜] ETF行情源={src} codes={codes} chg={out}")
        if src == "腾讯":
            em = self._fetch_em_changes(codes)
            if em:
                for c in codes:
                    if c in out and c in em and abs(out[c] - em[c]) > 0.3:
                        logger.warning(
                            f"[羊村基金涨跌榜] ETF行情源不一致 code={c} 腾讯={out[c]}% 东财={em[c]}%")
        return out

    def _fetch_intraday_curve(self, holds: list, fund_code: str = ""):
        """按持仓权重拼「今日走势」（相对昨收 %），并按交易时间对齐（A股/港股时段不同）。
        返回 (curve, kind)：kind='intraday' 盘中分时；'us' 美股最近时段；
        'nav' 近10日净值回退（分时不可用）；'' 完全无数据。
        分时接口每行形如 "0930 253.78 9882 2507854.00"，必须按空格切分取第 2 列。"""
        import urllib.request, json
        syms = [self._tx_symbol(tk) for tk, _, _ in holds]
        # 1) 昨收（曲线基准要和表格里的涨跌幅同口径）
        prev = {}
        try:
            req = urllib.request.Request(
                "https://qt.gtimg.cn/q=" + ",".join(syms),
                headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "ignore")
            for line in raw.strip().split(";"):
                line = line.strip()
                if not line or "=" not in line or "none_match" in line:
                    continue
                var, val = line.split("=", 1)
                f = val.strip('"').split("~")
                if len(f) > 4:
                    try:
                        prev[var.replace("v_", "").strip()] = float(f[4])
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass
        # 2) 各股分时 → {时间: 相对昨收 %}
        series = []
        for (tk, _, w), sym in zip(holds, syms):
            try:
                req = urllib.request.Request(
                    f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}",
                    headers={"User-Agent": "Mozilla/5.0"})
                d = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "ignore"))
                arr = d["data"][sym]["data"]["data"]
                pts = {}
                for row in arr:
                    parts = str(row).split()
                    if len(parts) < 2:
                        continue
                    try:
                        pts[parts[0]] = float(parts[1])
                    except ValueError:
                        continue
                if len(pts) < 2:
                    continue
                base = prev.get(sym) or next(iter(pts.values()))
                if not base or base <= 0:
                    continue
                series.append((w, {t: (v / base - 1) * 100 for t, v in pts.items()}))
            except Exception:
                continue
        if series:
            times = sorted(set().union(*[set(p) for _, p in series]))
            keys = [sorted(p.keys()) for _, p in series]
            idxs = [0] * len(series)
            last = [None] * len(series)
            curve = []
            for t in times:
                num = den = 0.0
                for i, (w, pts) in enumerate(series):
                    ks = keys[i]
                    # 收盘后沿用该股最后一个价（A股15:00收、港股16:08收，时区/收盘时间不同）
                    while idxs[i] < len(ks) and ks[idxs[i]] <= t:
                        last[i] = pts[ks[idxs[i]]]
                        idxs[i] += 1
                    if last[i] is not None:
                        num += w * last[i]
                        den += w
                if den:
                    curve.append(num / den)
            us_only = bool(syms) and all(s.startswith("us") for s in syms)
            return curve, ("us" if us_only else "intraday")
        # 回退：基金近10日净值走势
        if not fund_code:
            return [], ""
        try:
            url = (f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={fund_code}"
                   f"&pageIndex=1&pageSize=10")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                       "Referer": "https://fundf10.eastmoney.com/"})
            d = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "ignore"))
            rows = (d.get("Data", {}) or {}).get("LSJZList", [])
            navs = [float(r["DWJZ"]) for r in rows if r.get("DWJZ")]
            navs.reverse()  # 时间正序
            if len(navs) >= 2:
                base = navs[0]
                return [(v / base - 1) * 100 for v in navs], "nav"
        except Exception:
            pass
        return [], ""

    def _render_holdings_image(self, fname: str, rows: list, pred: float, curve: list,
                               curve_kind: str = "intraday",
                               cover: float | None = None) -> str | None:
        """rows: [(ticker,name,weight,chg)]，chg=None 表示行情没取到（显示「—」，也不参与加权）；
        curve: 走势（相对昨收 %）；curve_kind: intraday/us/nav/''；cover: 行情覆盖的仓位百分比。"""
        from PIL import Image, ImageDraw, ImageFont
        font_path = self._find_font()
        if not font_path:
            return None
        title_f = ImageFont.truetype(font_path, 30)
        sub_f = ImageFont.truetype(font_path, 15)
        txt_f = ImageFont.truetype(font_path, 20)
        name_f = ImageFont.truetype(font_path, 21)
        sm_f = ImageFont.truetype(font_path, 13)
        W, pad = 760, 30
        head_h = 88; row_h = 44; chart_h = 110; foot_h = 84
        H = head_h + row_h * (len(rows) + 1) + chart_h + foot_h + 10
        img = Image.new("RGB", (W, H), "#ffffff")
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, head_h], fill=(38, 50, 110))
        d.text((pad, 14), "重仓股实时涨跌", font=title_f, fill="#ffffff")
        short = fname if len(fname) <= 24 else fname[:24] + "…"
        d.text((pad, 54), short, font=sub_f, fill="#c8d0f0")
        y = head_h
        d.text((pad, y + 12), "#", font=txt_f, fill="#8a90a0")
        d.text((pad + 40, y + 12), "股票", font=txt_f, fill="#8a90a0")
        d.text((W - pad - 200, y + 12), "占净值", font=txt_f, fill="#8a90a0")
        d.text((W - pad - 90, y + 12), "实时涨跌", font=txt_f, fill="#8a90a0")
        y += row_h
        for i, (tk, nm, w, chg) in enumerate(rows, 1):
            if i % 2 == 0:
                d.rectangle([0, y, W, y + row_h], fill="#f7f9ff")
            d.text((pad, y + 11), str(i), font=txt_f, fill="#5b6270")
            d.text((pad + 40, y + 11), f"{nm}({tk})", font=name_f, fill="#20242e")
            d.text((W - pad - 200, y + 11), f"{w:.2f}%", font=txt_f, fill="#5b6270")
            if chg is None:
                d.text((W - pad - 90, y + 11), "—", font=name_f, fill="#b0b4c0")
            else:
                col = "#d93b34" if chg > 0 else ("#0e9f6e" if chg < 0 else "#8a90a0")
                d.text((W - pad - 90, y + 11), f"{chg:+.2f}%", font=name_f, fill=col)
            y += row_h
        # 今日盘中走势
        d.text((pad, y + 6), {"intraday": "今日盘中走势（相对昨收 %）",
                              "us": "最近美股时段走势（相对昨收 %）",
                              "nav": "近 10 个交易日净值走势（%，分时不可用）",
                              }.get(curve_kind, "今日盘中走势（相对昨收 %）"),
               font=txt_f, fill="#5b6270")
        cx0, cy0, cx1, cy1 = pad + 10, y + 30, W - pad - 10, y + chart_h - 6
        if len(curve) >= 2:
            vmin, vmax = min(curve), max(curve)
            span = (vmax - vmin) or 1
            pts = [(cx0 + (cx1 - cx0) * i / (len(curve) - 1),
                    cy1 - (cy1 - cy0) * (v - vmin) / span) for i, v in enumerate(curve)]
            up = curve[-1] >= curve[0]
            lc = "#d93b34" if up else "#0e9f6e"
            d.line(pts, fill=lc, width=2)
            d.polygon(pts + [(cx1, cy1), (cx0, cy1)],
                      fill=(245, 230, 230) if up else (225, 240, 232))
            d.text((cx0, cy0 - 2), f"{vmax:+.2f}%", font=sm_f, fill="#9aa0b0")
            d.text((cx1 - 46, cy1 - 16), f"{vmin:+.2f}%", font=sm_f, fill="#9aa0b0")
        else:
            d.text((cx0, cy0 + 30), "（暂无走势数据）",
                   font=sub_f, fill="#9aa0b0")
        y += chart_h
        d.line([(pad, y + 4), (W - pad, y + 4)], fill="#e6e9f2", width=1)
        pcol = "#d93b34" if pred > 0 else ("#0e9f6e" if pred < 0 else "#8a90a0")
        d.text((pad, y + 16), "重仓加权预估今日涨跌", font=txt_f, fill="#5b6270")
        d.text((W - pad - 180, y + 14), f"{pred:+.2f}%", font=title_f, fill=pcol)
        note = "重仓按最新季报 · 行情腾讯/新浪 · 仅供参考"
        if cover is not None and cover < 99.5:
            note = f"行情覆盖 {cover:.0f}% 持仓 · " + note
        d.text((pad, y + 56), note, font=sub_f, fill="#9aa0b0")
        out = os.path.join(str(_db_path().parent), "holdings_tmp.png")
        img.save(out, "PNG")
        return out

    # ---------- 盘中估值 ----------
    def _us_baseline_load(self) -> dict:
        """读「美股基准价」：{ticker: 价格}。每日美股收盘后重置一次，作为当天的起算点。"""
        try:
            raw = self._storage.get_meta("estimate_us_baseline") or ""
            data = json.loads(raw) if raw else {}
            return {str(k): float(v) for k, v in (data.get("px") or {}).items() if v}
        except Exception:
            return {}

    def _us_baseline_save(self, px: dict) -> None:
        try:
            self._storage.set_meta("estimate_us_baseline", json.dumps({
                "date": time.strftime("%Y-%m-%d"),
                "at": time.strftime("%H:%M"),
                "px": {k: round(float(v), 4) for k, v in px.items() if v},
            }, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 美股基准价写入失败: {e}")

    def _maybe_reset_us_baseline(self, now=None) -> None:
        """每天美股收盘后重置一次基准（自然日只重置一次）。

        重置点选在「08:00 隔夜美股班次推完之后」或「≥08:30」——此时美股已收盘（北京 04:00/05:00），
        拿到的现价就是昨夜收盘价。这样：
          · 当天 08:00 那张：用「昨天 08:30 的基准」算，正好是昨夜整场美股涨跌；
          · 08:30 之后（11:30/15:00/16:00）：基准已是昨夜收盘，美股未开盘 → 显示 0.00%，不再重复隔夜涨幅；
          · 夜里美股开盘后若在 08:00 前有变动，也会按相对昨夜收盘累计。
        美股正在交易（北京 21:30–05:00）时不重置，避免把盘中价当收盘价。"""
        now = now or time.localtime()
        today = time.strftime("%Y-%m-%d")
        try:
            raw = self._storage.get_meta("estimate_us_baseline") or ""
            if json.loads(raw).get("date") == today:
                return
        except Exception:
            pass
        mins = now.tm_hour * 60 + now.tm_min
        if mins >= 21 * 60 + 30 or mins < 5 * 60:
            return  # 美股交易中
        if mins < 8 * 60 + 30 and "08:00" not in self._push_done_load():
            return  # 08:00 班次还没发，先让它用昨天的基准算隔夜涨跌
        us: list[str] = []
        for r in self._storage.all_records():
            if not r["confirmed"] or (r["current"] or 0) <= 0.005:
                continue
            code = r["fund_code"]
            if _ETF_MAP.get(code):
                continue
            try:
                _fname, holds = self._fetch_fund_holdings(code)
            except Exception:
                continue
            for tk, _nm, _w in holds:
                if self._tx_symbol(tk).startswith("us"):
                    us.append(tk)
        us = list(dict.fromkeys(us))
        if not us:
            return
        q = self._fetch_stock_quotes(us)
        px = {t: v[1] for t, v in q.items() if v and v[1]}
        if not px:
            return
        self._us_baseline_save(px)
        _ESTIMATE_CACHE.clear()
        logger.info(f"[羊村基金涨跌榜] 美股基准价已重置（{len(px)} 只标的，"
                    f"{time.strftime('%H:%M')}）")

    def _fetch_fund_estimate(self, code: str):
        """估算单只基金当前盘中涨跌（带 60 秒缓存）。返回 (est_pct, cover, kind, fname)。"""
        hit = _ESTIMATE_CACHE.get(code)
        if hit and time.time() - hit[0] < ESTIMATE_TTL:
            return hit[1]
        return self._fetch_estimates_bulk([code]).get(code, (None, 0.0, "none", code))

    def _fetch_estimates_bulk(self, codes: list[str]) -> dict[str, tuple]:
        """批量估算多只基金：重仓股只抓一次（带 6h 缓存），所有标的行情合并成一次请求。
        返回 {code: (est_pct, cover, kind, fname)}；单只失败不影响其它。
        天天基金官方 fundgz 实时估值接口已于 2026-07 下线（指数基金），故用重仓行情拟合：
        指数/ETF联接按对应场内 ETF 近似，其余按最新季报重仓股实时加权，无个股盘面标 None。"""
        todo = [c for c in dict.fromkeys(codes) if c]
        if not todo:
            return {}
        now = time.time()
        out: dict[str, tuple] = {}
        pending = []
        for c in todo:
            hit = _ESTIMATE_CACHE.get(c)
            if hit and now - hit[0] < ESTIMATE_TTL:
                out[c] = hit[1]
            else:
                pending.append(c)
        if not pending:
            return out
        # 1) ETF 近似的那些：一次性批量取行情
        etf_codes = [c for c in pending if _ETF_MAP.get(c)]
        etf_chg: dict[str, float] = {}
        if etf_codes:
            try:
                etf_chg = self._fetch_etf_changes(list(dict.fromkeys(
                    _ETF_MAP[c] for c in etf_codes)))
            except Exception:
                etf_chg = {}
        # 2) 主动/混合/QDII：重仓股走缓存，所有 ticker 合并成一次行情请求
        stock_codes = [c for c in pending if not _ETF_MAP.get(c)]
        holdings: dict[str, tuple] = {}
        tickers: list[str] = []
        if stock_codes:
            from concurrent.futures import ThreadPoolExecutor
            def _hold(c):
                try:
                    return c, self._fetch_fund_holdings(c)
                except Exception:
                    return c, (c, [])
            # 重仓股走缓存，未命中的几只并发抓，别一只一只排队
            with ThreadPoolExecutor(max_workers=min(6, len(stock_codes))) as ex:
                for c, (fname, holds) in ex.map(_hold, stock_codes):
                    holdings[c] = (fname, holds)
                    tickers.extend(h[0] for h in holds)
        uniq = list(dict.fromkeys(tickers))
        quotes = self._fetch_stock_quotes(uniq) if uniq else {}
        chgs = {t: v[0] for t, v in quotes.items() if v[0] is not None}
        # 美股标的：白天休市时 f[32] 仍是「昨夜那一场」的涨跌，直接用会让隔夜涨幅
        # 在 11:30/15:00/16:00 三张图里重复出现。这里改用「每日美股收盘后重置的基准价」，
        # 只累计相对上一次重置（= 上一个美股收盘）以来的变化。
        us_base = self._us_baseline_load()
        pxs = {t: v[1] for t, v in quotes.items()
               if v[1] and us_base.get(t) and self._tx_symbol(t).startswith("us")}

        def _val(tk: str):
            b = us_base.get(tk)
            p = pxs.get(tk)
            if b and p and b > 0:
                return round((p / b - 1) * 100, 2)
            return chgs.get(tk)

        for c in pending:
            etf = _ETF_MAP.get(c)
            if etf:
                chg = etf_chg.get(etf)
                res = ((round(chg, 2), 100.0, "etf", c) if chg is not None
                       else (None, 0.0, "none", c))
            else:
                fname, holds = holdings.get(c, (c, []))
                if not holds:
                    res = (None, 0.0, "none", fname)
                else:
                    num = den = cover = 0.0
                    us_cnt = us_all = 0
                    for tk, _nm, w in holds:
                        if self._tx_symbol(tk).startswith("us"):
                            us_all += 1
                        us_cnt += 1
                        v = _val(tk)
                        if v is not None:
                            num += w * v
                            den += w
                            cover += w
                    # 全是美股标的（QDII）→ kind="us"，盘前/A股休市时不该被当成 0.00 盘面
                    kind = "us" if (holds and us_all == us_cnt) else "stock"
                    res = ((round(num / den, 2), round(cover, 1), kind, fname)
                           if den else (None, round(cover, 1), "none", fname))
            out[c] = res
            _ESTIMATE_CACHE[c] = (now, res)
        return out

    def _render_estimate_image(self, member: str, items: list, total_pct,
                               phase: str = "") -> str | None:
        """渲染『盘中估值』图片：成员全部基金当前盘中涨跌 + 持仓加权总预估。
        items: [{fund_code,fund_name,current,est,kind}]，est=None 表示无盘面（债券/指数）。"""
        from PIL import Image, ImageDraw, ImageFont
        font_path = self._find_font()
        if not font_path:
            return None
        title_f = ImageFont.truetype(font_path, 30)
        sub_f = ImageFont.truetype(font_path, 15)
        txt_f = ImageFont.truetype(font_path, 20)
        name_f = ImageFont.truetype(font_path, 21)
        sm_f = ImageFont.truetype(font_path, 13)
        W, pad = 760, 30
        head_h = 88; row_h = 44; foot_h = 96
        # 有盘面在前（按估算降序），无盘面排后
        ordered = [it for it in items if it["est"] is not None]
        ordered.sort(key=lambda x: x["est"], reverse=True)
        ordered += [it for it in items if it["est"] is None]
        H = head_h + row_h * (len(ordered) + 1) + foot_h + 10
        img = Image.new("RGB", (W, H), "#ffffff")
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, head_h], fill=(38, 50, 110))
        d.text((pad, 14), "已收盘 · 持仓当日预估" if phase else "盘中估值 · 持仓预估涨跌",
               font=title_f, fill="#ffffff")
        d.text((pad, 54), member + (f"   · {phase} 锁定" if phase else ""), font=sub_f, fill="#c8d0f0")
        y = head_h
        d.text((pad, y + 12), "#", font=txt_f, fill="#8a90a0")
        d.text((pad + 40, y + 12), "基金", font=txt_f, fill="#8a90a0")
        d.text((W - pad - 200, y + 12), "现市值", font=txt_f, fill="#8a90a0")
        d.text((W - pad - 90, y + 12), "收盘估算" if phase else "盘中估算", font=txt_f, fill="#8a90a0")
        y += row_h
        for i, it in enumerate(ordered, 1):
            if i % 2 == 0:
                d.rectangle([0, y, W, y + row_h], fill="#f7f9ff")
            d.text((pad, y + 11), str(i), font=txt_f, fill="#5b6270")
            nm = it["fund_name"] or it["fund_code"] or ""
            nm = nm if len(nm) <= 18 else nm[:18] + "…"
            d.text((pad + 40, y + 10), f"{nm}  ({it['fund_code']})", font=name_f, fill="#20242e")
            d.text((W - pad - 200, y + 11), f"¥{it['current']:,.2f}", font=txt_f, fill="#5b6270")
            est = it["est"]
            if est is None:
                d.text((W - pad - 90, y + 11), "—", font=name_f, fill="#b0b4c0")
            else:
                col = "#d93b34" if est > 0 else ("#0e9f6e" if est < 0 else "#8a90a0")
                d.text((W - pad - 90, y + 11), f"{est:+.2f}%", font=name_f, fill=col)
            y += row_h
        d.line([(pad, y + 4), (W - pad, y + 4)], fill="#e6e9f2", width=1)
        d.text((pad, y + 16), "持仓加权预估今日涨跌", font=txt_f, fill="#5b6270")
        if total_pct is None:
            d.text((W - pad - 180, y + 14), "无盘面基金", font=title_f, fill="#8a90a0")
        else:
            pcol = "#d93b34" if total_pct > 0 else ("#0e9f6e" if total_pct < 0 else "#8a90a0")
            d.text((W - pad - 180, y + 14), f"{total_pct:+.2f}%", font=title_f, fill=pcol)
        d.text((pad, y + 56), "估算=最新季报重仓股实时加权 · 仅股票盘面 · 仅供参考", font=sub_f, fill="#9aa0b0")
        d.text((pad, y + 76), "— 表示债券型无盘面；指数基金按对应场内 ETF 实时行情近似（官方估值已下线）", font=sm_f, fill="#9aa0b0")
        out = os.path.join(str(_db_path().parent), "estimate_tmp.png")
        img.save(out, "PNG")
        return out

    def _render_fund_picker_image(self, member: str, recs: list) -> str | None:
        """渲染选基列表图片，避免群里文字刷屏。"""
        from PIL import Image, ImageDraw, ImageFont
        font_path = self._find_font()
        if not font_path:
            return None
        title_f = ImageFont.truetype(font_path, 26)
        sub_f = ImageFont.truetype(font_path, 14)
        txt_f = ImageFont.truetype(font_path, 18)
        name_f = ImageFont.truetype(font_path, 19)
        W, pad = 720, 28
        head_h = 80; row_h = 42; foot_h = 56
        H = head_h + row_h * len(recs) + foot_h + 10
        img = Image.new("RGB", (W, H), "#ffffff")
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, head_h], fill=(38, 50, 110))
        d.text((pad, 14), "重仓股查询 · 选一只基金", font=title_f, fill="#ffffff")
        d.text((pad, 48), f"{member} 的持仓，@机器人 回复编号", font=sub_f, fill="#c8d0f0")
        y = head_h
        for i, r in enumerate(recs, 1):
            if i % 2 == 0:
                d.rectangle([0, y, W, y + row_h], fill="#f7f9ff")
            d.text((pad, y + 10), str(i), font=txt_f, fill="#d93b34")
            nm = r.get("fund_name") or r.get("fund_code") or ""
            nm = nm if len(nm) <= 20 else nm[:20] + "…"
            d.text((pad + 36, y + 9), f"{nm}  ({r.get('fund_code','')})", font=name_f, fill="#20242e")
            y += row_h
        d.text((pad, y + 14), "@机器人 回复编号数字，或「取消」", font=sub_f, fill="#9aa0b0")
        out = os.path.join(str(_db_path().parent), "picker_tmp.png")
        img.save(out, "PNG")
        return out

    def _load_avatar(self, member: str, size: int):
        """按成员名从 avatars 目录加载真实头像，裁剪为圆形 RGBA 图；找不到返回 None。"""
        path = os.path.join(AVATAR_DIR, _safe_filename(member) + ".jpg")
        if not os.path.exists(path):
            return None
        try:
            from PIL import Image, ImageDraw
            im = Image.open(path).convert("RGBA")
            im = im.resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
            out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            out.paste(im, (0, 0), mask)
            return out
        except Exception as e:
            logger.error(f"[羊村基金涨跌榜] 加载头像失败 {member}: {e}")
            return None

    async def _fill_codes(self, records: list[dict]) -> int:
        """对缺基金代码但有名称的记录，按名称自动补代码。返回补到的数量。
        先走天天基金精确/相似匹配；仍补不到的，用大模型把噪声名称规范化后再匹配。"""
        filled = 0
        pending = [r for r in records if not r.get("fund_code") and r.get("fund_name")]
        if not pending:
            return 0
        # 1) 规则匹配（精确/相似度）
        for rec in pending:
            code = await asyncio.to_thread(_search_fund_code, rec["fund_name"])
            if code:
                rec["fund_code"] = code
                filled += 1
        # 2) 仍未补上的，用大模型规范化名称后再匹配
        left = [r for r in pending if not r.get("fund_code")]
        if left:
            try:
                norm = await _llm_normalize_fund_names([r["fund_name"] for r in left])
            except Exception as e:
                logger.warning(f"[羊村基金涨跌榜] 名称规范化失败: {e}")
                norm = []
            for rec, nn in zip(left, norm):
                if nn and nn != rec["fund_name"]:
                    code = await asyncio.to_thread(_search_fund_code, nn)
                    if code:
                        rec["fund_code"] = code
                        filled += 1
        return filled

    # ---------- 对账：截图 / 口语 让大模型改库 ----------

    async def _recon_start(self, event, umo: str, sender: str, text: str,
                           has_img: bool) -> str:
        """「对账」入口：有截图就按图校正，没截图就用大模型解析口语指令。"""
        member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
        recs = [r for r in self._storage.all_records()
                if self._member_matches(r["member"], member)]
        ops: list[dict] = []
        ignored: list[dict] = []
        convs: list[dict] = []
        source = ""
        stamp = time.strftime("%Y-%m-%d")
        if has_img:
            img = next((c for c in event.get_messages() if isinstance(c, Image)), None)
            path = (await _resolve_image_async(img)) if img is not None else None
            if not path:
                return "拿不到截图内容，重发一张，或改用文字：『对账 永赢医药 本金 500』。"
            ocr, ocr_date, order = [], "", {}
            try:
                res_hold, res_conv = await asyncio.gather(
                    _ocr_image(path), _ocr_convert_order(path), return_exceptions=True)
                if isinstance(res_hold, Exception):
                    raise res_hold
                ocr, ocr_date, _page = res_hold
                if isinstance(res_conv, dict):
                    order = res_conv
            except Exception as e:
                logger.error(f"[羊村基金涨跌榜] 对账识图失败: {e}")
                return f"图像识别失败：{e}"
            finally:
                try:
                    if (path and os.path.exists(path)
                            and not str(path).startswith(str(getattr(img, "file", "")))):
                        os.remove(path)
                except OSError:
                    pass
            if order and str(order.get("kind") or "") == "convert":
                return self._propose_convert_order(umo, sender, member, recs, order)
            if not ocr:
                return "识别不到基金数据，请发清晰的持仓页截图，或改用文字指令。"
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", (ocr_date or "").strip()):
                stamp = ocr_date.strip()
            try:
                ops, ignored, convs = self._diff_ocr_ops(member, recs, ocr,
                                                         self._storage.active_conversions(member))
            except Exception as e:
                logger.warning(f"[羊村基金涨跌榜] 截图对账比对失败: {e}")
                convs = []
            source = "截图"
            if not ops and not [x for x in convs if x["amount"] > 0.005]:
                return (f"截图里 {len(ocr)} 条基金和台账现值一致，不用改。\n"
                        f"（想强制覆盖就说「对账 基金名 现值 金额」。）")
        if not ops:
            ops = await _llm_parse_recon_ops(text, recs)
            source = "指令"
            for o in ops:
                if o.get("kind") != "set":
                    continue
                rec = next((r for r in recs if r["fund_code"] == o["code"]), None)
                if not rec:
                    continue
                o.setdefault("old_current", float(rec["current"] or 0.0))
                o.setdefault("old_principal", float(rec["principal"] or 0.0))
                o["name"] = rec["fund_name"] or o.get("name") or o["code"]
            if not ops:
                # 只说了「对帐」没图也会走到这里 → 挂个等待态，下一条截图直接进对账
                self._recon_pending[(umo, sender)] = {
                    "member": member, "ops": [], "ignored": [], "convs": [],
                    "source": "等待截图", "stamp": stamp}
                self._state_ts.setdefault((umo, sender), {})["recon"] = time.time()
                return ("把持仓截图发给我，我按图对账（先给你看差异，回「确认」才写库）。\n"
                        "不想发图就直接说：「对账 016702 现值 205」「对账 永赢医药 本金 500」。\n"
                        "　（取消就说「取消」）")
        self._recon_pending[(umo, sender)] = {
            "member": member, "ops": ops, "ignored": ignored,
            "convs": convs, "source": source, "stamp": stamp}
        self._state_ts.setdefault((umo, sender), {})["recon"] = time.time()
        return self._render_recon_plan(member, ops, source, ignored, convs)

    async def _try_convert_shot(self, event, umo: str, sender: str) -> str | None:
        """截图里是「基金转换」订单就直接给登记方案；不是转换单就返回 None（照常交给别的识图）。"""
        img = next((c for c in event.get_messages() if isinstance(c, Image)), None)
        if img is None:
            return None
        path = await _resolve_image_async(img)
        if not path:
            return None
        try:
            order = await _ocr_convert_order(path)
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 转换截图识别失败: {e}")
            return None
        finally:
            try:
                if (path and os.path.exists(path)
                        and not str(path).startswith(str(getattr(img, "file", "")))):
                    os.remove(path)
            except OSError:
                pass
        if not isinstance(order, dict) or str(order.get("kind") or "") != "convert":
            return None
        member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
        recs = [r for r in self._storage.all_records()
                if self._member_matches(r["member"], member)]
        return self._propose_convert_order(umo, sender, member, recs, order)

    # ---------- 买入 / 卖出交易截图（识别 → 确认方案 → 落库） ----------

    @staticmethod
    def _parse_shares(v) -> float | None:
        try:
            if v is None or v == "":
                return None
            f = float(v)
            if f < 0 or abs(f) >= 1e8:
                return None
            return round(f, 6)
        except (TypeError, ValueError):
            return None

    async def _propose_order_shot(self, event, umo: str, sender: str) -> str | None:
        """买入/卖出交易截图：识别后生成确认方案，回「确认」才落库；非买/卖截图返回 None。"""
        img = next((c for c in event.get_messages() if isinstance(c, Image)), None)
        if img is None:
            return None
        path = await _resolve_image_async(img)
        if not path:
            return None
        try:
            order = await _ocr_order_op(path)
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 交易截图识别失败: {e}")
            return None
        finally:
            try:
                if (path and os.path.exists(path)
                        and not str(path).startswith(str(getattr(img, "file", "")))):
                    os.remove(path)
            except OSError:
                pass
        if not isinstance(order, dict):
            return None
        kind = str(order.get("kind") or "").strip().lower()
        if kind not in ("buy", "sell"):
            return None
        member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
        recs = [r for r in self._storage.all_records()
                if self._member_matches(r["member"], member)]
        name = str(order.get("fund_name") or "").strip()
        code = str(order.get("fund_code") or "").strip()
        if not re.fullmatch(r"\d{6}", code or ""):
            code = _match_fund_code(name, recs) or ""
        if not code:
            got = _resolve_fund(name)
            if got:
                code, name = got[0], got[1] or name
        if not code:
            verb = "买入" if kind == "buy" else "卖出"
            return (f"认出了这张{verb}截图，但没对上基金：『{name or '?'}』。\n"
                    "先「导入数据」把它录进台账，或改用文字指令操作。")
        amount = _num_or_none(order.get("amount"), allow_negative=False)
        shares = self._parse_shares(order.get("shares"))
        status = str(order.get("status") or "").strip()
        apply_time = str(order.get("apply_time") or "").strip()
        confirm_by = str(order.get("confirm_by") or "").strip()
        st = {"member": member, "op": kind, "code": code, "name": name or code,
              "amount": amount, "shares": shares, "status": status,
              "apply_time": apply_time, "confirm_by": confirm_by}
        self._order_select[(umo, sender)] = st
        self._state_ts.setdefault((umo, sender), {})["order"] = time.time()
        return self._render_order_plan(st)

    def _render_order_plan(self, st: dict) -> str:
        verb = "买入" if st["op"] == "buy" else "卖出"
        lines = [f"🧾 识别到一笔{verb}交易，先核对，回「确认」才入账："]
        lines.append(f"　基金：{st['name']}({st['code']})")
        amt = f"¥{st['amount']:,.2f}" if st["amount"] is not None else "金额待确认"
        shr = f"{st['shares']:,.4f} 份" if st["shares"] is not None else "份额待确认"
        lines.append(f"　金额：{amt}　｜　份额：{shr}")
        meta = []
        if st.get("status"):
            meta.append(f"状态：{st['status']}")
        if st.get("confirm_by"):
            meta.append(f"预计 {st['confirm_by']} 前确认/到账")
        if st.get("apply_time"):
            meta.append(f"时间：{st['apply_time'][:16]}")
        if meta:
            lines.append("　" + "　".join(meta))
        if st["op"] == "sell" and self._order_in_transit(st):
            lines.append("　（金额/份额确认前先不扣本金，等确认后再按实际到手额扣减并结转盈亏）")
        lines.append("回「确认」登记这笔；回「取消」放弃。")
        return "\n".join(lines)

    @staticmethod
    def _order_in_transit(st: dict) -> bool:
        return bool(re.search(r"待确认|确认中|进行中|申购中|赎回中|待份额", st.get("status") or ""))

    def _commit_order(self, st: dict) -> str:
        member, op, code, name = st["member"], st["op"], st["code"], st["name"]
        amount = st.get("amount")
        shares = st.get("shares")
        if op == "buy":
            if amount is None or amount <= 0:
                return ("截图里没识别到买入金额，暂未入账；"
                        "可文字发「买入基金 基金名 金额」补充。")
            _affected, is_new = self._storage.buy_increase(member, code, name, amount)
            return (f"✅ 已登记买入 {name}({code}) ¥{amount:,.2f}，"
                    "份额确认后并入本金。\n回复「涨跌榜」查看数据。")
        # 卖出
        if self._order_in_transit(st):
            # 有份额/金额就先把份额冻结出持仓，避免「App 已卖、台账还满仓」
            ok, frz_sh, frz_c, part, err = self._storage.freeze_sell(
                member, code, name, shares or 0.0, amount or 0.0,
                st.get("confirm_by") or "")
            self._storage.add_trade_log(member, code, name,
                                        "卖出挂单" if ok else "卖出待确认", frz_c or 0.0)
            if not ok:
                return (f"⏳ 已记下这笔在途卖出 {name}({code})"
                        f"（状态：{st.get('status') or '金额待确认'}）。\n"
                        f"{err}到账后发到账截图，或回「卖出基金 编号 金额」补账。")
            body = (f"⏳ 已挂出在途卖出 {name}({code})"
                    f"（状态：{st.get('status') or '金额待确认'}）。\n")
            if frz_sh > 0:
                body += (f"　已冻结 {frz_sh:,.4f} 份，约 ¥{frz_c:,.2f}，对应成本 ¥{part:,.2f}\n"
                         "　这笔钱暂时不算进持仓市值，等到账确认后再按到手额结转盈亏。\n")
            else:
                body += ("　份额/金额都还没识别到，先只记录挂单；发到账截图后再按比例扣减。\n")
            return body + "到账后回「确认」或发卖出到账截图即可结算。"
        if amount is None or amount <= 0:
            return "截图里没识别到卖出到手金额，暂未扣账；可文字发「卖出基金 编号 金额」补充。"
        # 已有在途挂单 → 这笔到账就是在结算那一单
        pend = self._storage.pending_sells(member, code)
        if pend:
            p = pend[0]
            ok, pnl, cum, err = self._storage.settle_sell(p["id"], amount)
            if ok > 0:
                self._storage.add_trade_log(member, code, name, "卖出", amount)
                return (f"✅ 已结算在途卖出 {name}({code})，到账 ¥{amount:,.2f}，"
                        f"冻结 {p['shares']:,.4f} 份。\n"
                        f"📊 本次卖出盈亏 {pnl:+,.2f}，该基金累计已实现 {cum:+,.2f}。")
            if ok < 0:
                pass  # 未冻结的挂单 → 走普通卖出
            else:
                return f"卖出结算失败：{err}"
        rows, new_p, pnl, realized, err = self._storage.sell_decrease(member, code, amount)
        if rows <= 0:
            return f"卖出失败：{err or '记录不存在。'}"
        self._storage.add_trade_log(member, code, name, "卖出", amount)
        # 该基金还有别的在途卖出挂单（未冻结的那条）→ 顺手关掉，免得重复扣
        for p in self._storage.pending_sells(member, code):
            if float(p["principal_part"] or 0) <= 0 and float(p["shares"] or 0) <= 0:
                self._storage.close_sell(p["id"])
        return (f"✅ 已卖出 {name}({code}) ¥{amount:,.2f}，"
                f"剩余已确认本金 ¥{new_p:,.2f}。\n"
                f"📊 本次卖出盈亏 {pnl:+,.2f}，该基金累计已实现 {realized:+,.2f}。")

    def _propose_convert_order(self, umo: str, sender: str, member: str,
                               recs: list[dict], order: dict) -> str:
        """「转换订单」截图 → 生成在途转换登记方案（确认后才落库）。"""
        src_name = (order.get("src_name") or "").strip()
        tgt_name = (order.get("tgt_name") or "").strip()
        src_code = str(order.get("src_code") or "").strip()
        tgt_code = str(order.get("tgt_code") or "").strip()
        if not re.fullmatch(r"\d{6}", src_code or ""):
            src_code = _match_fund_code(src_name, recs) or ""
        if not re.fullmatch(r"\d{6}", tgt_code or ""):
            tgt_code = _match_fund_code(tgt_name, recs) or ""
        if not src_code:
            got = _resolve_fund(src_name)
            src_code = got[0] if got else ""
        if not tgt_code:
            got = _resolve_fund(tgt_name)
            tgt_code = got[0] if got else ""
        if not src_code or not tgt_code:
            return ("认出了转换订单，但没对上基金代码：\n"
                    f"　转出『{src_name or '?'}』　转入『{tgt_name or '?'}』\n"
                    "先「导入数据」把这两只录进台账，或回「转换基金」手动登记。")
        if src_code == tgt_code:
            return "截图里转出和转入是同一只基金，没法登记。"
        update_id = 0
        for c in self._storage.active_conversions(member):
            if c["src_code"] == src_code:
                if not _num_or_none(order.get("shares")):
                    return (f"【{c['src_name'] or src_code}】已经有一笔在途转换（#{c['id']}），"
                            f"不重复登记。回「转换状态」看进度。")
                # 截图带份额：拿它来校正那笔在途（当初可能按金额登记成了别的数）
                update_id = int(c["id"])
        rec = next((r for r in recs if r["fund_code"] == src_code), None)
        if rec is None:
            return (f"台账里没有【{src_name}】({src_code})，"
                    f"先「导入数据」把它录进来再登记转换。")
        shares = _num_or_none(order.get("shares"))
        apply_time = str(order.get("apply_time") or "")
        try:
            at = datetime.strptime(apply_time[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            at = datetime.now()
        out_date = _default_convert_out_date(at)
        anchor = self._convert_anchor()
        qdii = ("QDII" in tgt_name.upper()) or ("美元" in tgt_name)
        in_by = str(order.get("in_confirm_by") or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", in_by):
            confirm_at = f"{in_by} {anchor}"
            confirm_note = f"订单预计 {in_by} 完成转入确认"
        else:
            confirm_at = _default_convert_confirm_at(out_date, qdii, anchor)
            confirm_note = "QDII T+2" if qdii else "T+1"
        ratio, est, nav_note = 0.0, 0.0, ""
        cur = float(rec["current"] or 0.0)
        sh_total = float(rec.get("shares") or 0.0)
        if shares and cur > 0:
            nav = _fetch_latest_nav(src_code)
            if nav:
                est = round(shares * nav[1], 2)
                ratio = (min(round(shares / sh_total, 6), 1.0) if sh_total > 0
                         else min(round(est / cur, 6), 1.0))
                nav_note = (f"（按 {nav[0]} 净值 {nav[1]:.4f} 估算 ≈ ¥{est:,.2f}，"
                            f"占持仓 {sh_total:,.2f} 份的 {ratio * 100:.1f}%）"
                            if sh_total > 0 else
                            f"（按 {nav[0]} 净值 {nav[1]:.4f} 估算 ≈ ¥{est:,.2f}）")
        if not shares:
            nav_note = "（截图没读到份额，先按整只登记，可用「转出份额 X」改）"
        in_nav = _num_or_none(order.get("in_nav")) or 0.0
        in_shares = _num_or_none(order.get("in_shares")) or 0.0
        if in_shares <= 0:
            in_nav = 0.0
        self._recon_pending[(umo, sender)] = {
            "kind": "convert_order", "member": member, "ops": [], "ignored": [],
            "convs": [], "source": "转换订单截图", "stamp": time.strftime("%Y-%m-%d"),
            "order": {"update_id": update_id,
                      "src_code": src_code, "src_name": rec["fund_name"] or src_name,
                      "tgt_code": tgt_code, "tgt_name": tgt_name,
                      "out_date": out_date, "confirm_at": confirm_at,
                      "part_ratio": ratio, "part_amount": est,
                      "out_shares": float(shares or 0.0),
                      "in_nav": in_nav, "in_shares": in_shares,
                      "shares": shares, "est": est}}
        self._state_ts.setdefault((umo, sender), {})["recon"] = time.time()
        scope = (f"申请转出 {shares:.2f} 份{nav_note}" if shares else "份额未识别")
        _amt = est if est > 0 else round(cur * ratio, 2)
        plan_amt = (f"约 ¥{_amt:,.2f}（{ratio * 100:.1f}%）" if ratio > 0
                    else f"整只 ¥{cur:,.2f}")
        if in_shares > 0:
            plan_amt += f" → 转入 {in_shares:,.2f} 份（净值 {in_nav:.4f}）"
        head = ("🔄 用截图校正在途转换 #%d：按份额转换，份额×转出日净值=金额，"
                "金额÷确认日净值=转入份额\n" % update_id) if update_id else \
               "🔄 转换订单（截图）：按份额转换，份额×转出日净值=金额，金额÷确认日净值=转入份额\n"
        body = (f"　转出 【{rec['fund_name'] or src_name}】({src_code})\n"
                f"　转入 【{tgt_name}】({tgt_code})\n"
                f"　申请时间 {at:%Y-%m-%d %H:%M} · {scope}\n"
                f"　净值日 {out_date}（T 日，按这天净值算钱；支付宝的次日才是「确认完成」）\n"
                f"　份额确认 {confirm_at}（{confirm_note}）\n"
                f"　转出金额 {plan_amt}\n"
                "回「确认」登记为在途转换；「取消」放弃。")
        return head + body

    def _commit_order_conversion(self, st: dict, umo: str, sender: str) -> str:
        o = st["order"]
        if o.get("update_id"):
            cid = int(o["update_id"])
            con = _db_connect(_db_path())
            try:
                con.execute(
                    "UPDATE conversions SET tgt_code=?, tgt_name=?, out_date=?, confirm_at=?, "
                    "part_amount=?, part_ratio=?, out_shares=? WHERE id=? AND status='active'",
                    (o["tgt_code"], o["tgt_name"], o["out_date"], o["confirm_at"],
                     round(float(o.get("part_amount") or 0.0), 2),
                     round(float(o.get("part_ratio") or 0.0), 6),
                     round(float(o.get("out_shares") or 0.0), 4), cid))
                con.commit()
            finally:
                con.close()
            if (o.get("in_shares") or 0) > 0:
                self._storage.set_conversion_in(
                    cid, o.get("in_nav") or 0.0, o.get("in_shares") or 0.0)
            self._recon_pending.pop((umo, sender), None)
            self._state_ts.pop((umo, sender), None)
            return (f"✅ 已用在途转换 #{cid} 改成按截图：转出 "
                    f"{float(o.get('out_shares') or 0):,.2f} 份"
                    f"（≈¥{float(o.get('part_amount') or 0):,.2f}），转出日 {o['out_date']}。\n"
                    f"　回「转换状态」核对。")
        cid = self._storage.create_conversion(
            st["member"], o["src_code"], o["src_name"], o["tgt_code"], o["tgt_name"],
            o["out_date"], o["confirm_at"],
            part_amount=o.get("part_amount") or 0.0,
            part_ratio=o.get("part_ratio") or 0.0,
            out_shares=o.get("out_shares") or 0.0)
        if (o.get("in_shares") or 0) > 0:
            self._storage.set_conversion_in(
                cid, o.get("in_nav") or 0.0, o.get("in_shares") or 0.0)
        self._recon_pending.pop((umo, sender), None)
        self._state_ts.pop((umo, sender), None)
        if float(o.get("out_shares") or 0) > 0:
            pct = f"转出 {float(o['out_shares']):,.2f} 份（≈¥{float(o.get('part_amount') or 0):,.2f}）"
        elif o.get("part_ratio"):
            pct = f"转出 {o['part_ratio'] * 100:.1f}%"
        else:
            pct = "整只转出"
        return (f"✅ 已按订单登记在途转换 #{cid}（{pct}）\n"
                f"　【{o['src_name']}({o['src_code']})】→【{o['tgt_name']}({o['tgt_code']})】\n"
                f"　转出日 {o['out_date']} 当晚自动结算并挂账，"
                f"{o['confirm_at']} 提示你确认份额。\n"
                f"　回「转换状态」看进度。")

    def _diff_ocr_ops(self, member: str, recs: list[dict], ocr: list[dict],
                      active: list[dict] | None = None):
        """把截图识别结果和台账逐条比对，产出改动清单（只改有差异的）。
        有在途转换的源/转入基金不直接覆盖，改为「转换结算候选」单独处理，
        否则会把 App 里已经搬走的钱当成盈亏、或让转换结算拿一条被清零的记录去算。"""
        active = active or []
        ocr_by_code = {}
        for o in ocr:
            amt = _num_or_none(o.get("amount")) or 0.0
            code = str(o.get("fund_code") or "").strip()
            name = str(o.get("fund_name") or "").strip()
            if code and not re.fullmatch(r"\d{6}", code):
                code = ""
            if not code and name:
                code = _match_fund_code(name, recs)
            if code:
                ocr_by_code.setdefault(code, amt)
        by_code = {r["fund_code"]: r for r in recs}
        # 先判定每笔在途转换「App 到底执行了没有」：只有源基金跌幅接近转出比例才算执行，
        # 否则那点差额只是净值涨跌（今天 002053 -4.55% 差点被当成「已转走 50%」结算掉）。
        exec_map: dict = {}
        for c in active:
            src_c = by_code.get(c["src_code"])
            if not src_c:
                continue
            full_c = round(float(src_c["current"] or 0.0), 2)
            ratio_c = float(c.get("part_ratio") or 0.0)
            # 只有 part_amount、没记比例的老/手工数据：按当前现值反推比例，
            # 否则会被当成「整只转出」，一半的减值就识别不出来。
            if ratio_c <= 0 and full_c > 0:
                pa_c = float(c.get("part_amount") or 0.0)
                if pa_c > 0:
                    ratio_c = min(pa_c / full_c, 1.0)
            ocr_c = ocr_by_code.get(c["src_code"])
            if ocr_c is None:
                done = ratio_c <= 0          # 整只转换：截图里没这只了才算执行
            elif full_c > 0:
                drop = (full_c - ocr_c) / full_c
                done = (drop > max(0.15, ratio_c * 0.6)) if ratio_c > 0 \
                    else (ocr_c < full_c * 0.05)
            else:
                done = False
            exec_map[c["id"]] = done
        blocked: set = set()
        for c in active:
            if exec_map.get(c["id"]):
                blocked.add(c["src_code"])
                blocked.add(c["tgt_code"])
        ops: list[dict] = []
        convs: list[dict] = []
        seen: set = set()
        seen_conv: set = set()
        for o in ocr:
            amt = _num_or_none(o.get("amount")) or 0.0
            if amt <= 0:
                continue
            name = str(o.get("fund_name") or "").strip()
            code = str(o.get("fund_code") or "").strip()
            if code and not re.fullmatch(r"\d{6}", code):
                code = ""
            old = by_code.get(code) if code else None
            if old is None and name:
                matched = _match_fund_code(name, recs)
                if matched:
                    code, old = matched, by_code.get(matched)
            if old is None and not code and name:
                try:
                    got = _resolve_fund(name)
                except Exception:
                    got = None
                if got:
                    code = got[0]
            if not code:
                continue
            # App 已执行转换的源/转入基金 → 交给结算处理，不在这里改金额
            if code in blocked:
                seen_conv.add(code)
                continue
            seen.add(code)
            # 在途转换的「源基金」：支付宝的持有收益只算没被转走的那一半，
            # 拿它反推成本会把另一半成本丢掉 → 只对齐现值，成本留给结算按比例结转
            conv_of_code = next((c for c in active if c["src_code"] == code), None)
            if conv_of_code is not None and old is not None:
                oldcur_c = float(old["current"] or 0.0)
                if abs(amt - oldcur_c) >= 0.005:
                    ops.append({"kind": "set", "code": code,
                                "name": old["fund_name"] or name or code,
                                "principal": float(old["principal"] or 0.0),
                                "current": round(amt, 2),
                                "old_principal": float(old["principal"] or 0.0),
                                "old_current": oldcur_c})
                continue
            if old is None:
                income = _num_or_none(o.get("income"), allow_negative=True)
                has_income = income is not None and -amt < income < amt
                principal = round(amt - income, 2) if has_income else round(amt, 2)
                ops.append({"kind": "insert", "code": code, "name": name or code,
                            "principal": max(principal, 0.0), "current": round(amt, 2)})
                continue
            oldcur = float(old["current"] or 0.0)
            if abs(amt - oldcur) < 0.005:
                continue
            income = _num_or_none(o.get("income"), allow_negative=True)
            prin = float(old["principal"] or 0.0)
            if income is not None and -amt < income < amt:
                prin = round(amt - income, 2)
            ops.append({"kind": "set", "code": code, "name": old["fund_name"] or name or code,
                        "principal": prin, "current": round(amt, 2),
                        "old_principal": float(old["principal"] or 0.0),
                        "old_current": oldcur})
        for c in active:
            src = by_code.get(c["src_code"])
            if not src:
                continue
            full = round(float(src["current"] or 0.0), 2)
            ocr_src = ocr_by_code.get(c["src_code"])
            done = bool(exec_map.get(c["id"]))
            amount = (round(max(full - (ocr_src or 0.0), 0.0), 2) if done else 0.0)
            convs.append({
                "cid": c["id"], "src_code": c["src_code"], "src_name": c["src_name"],
                "tgt_code": c["tgt_code"], "tgt_name": c["tgt_name"],
                "out_date": c["out_date"], "confirm_at": c["confirm_at"],
                "full": full, "ocr_src": ocr_src, "amount": amount,
                "executed": done,
                "ocr_tgt": ocr_by_code.get(c["tgt_code"]),
            })
        ignored = [r for r in recs
                   if r["fund_code"] not in seen and r["fund_code"] not in seen_conv
                   and float(r["current"] or 0) > 0.005]
        return ops, ignored, convs

    def _render_recon_plan(self, member: str, ops: list[dict], source: str,
                           ignored: list[dict], convs: list[dict] | None = None) -> str:
        convs = convs or []
        lines = [f"🧾 对账 · {source} · {member}"]
        for i, o in enumerate(ops, 1):
            tag = f"{o['name']}({o['code']})"
            if o["kind"] == "delete":
                lines.append(f"  {i}. 🗑 删除 {tag}")
            elif o["kind"] == "insert":
                cur = float(o.get("current") or 0.0)
                pri = float(o.get("principal") or 0.0)
                lines.append(f"  {i}. 🆕 {tag} 现值 ¥{cur:,.2f}"
                             f" / 成本 ¥{pri:,.2f}")
            else:
                oc = float(o.get("old_current") or 0.0)
                op_ = float(o.get("old_principal") or 0.0)
                cur = o.get("current")
                pri = o.get("principal")
                bits = []
                if cur is not None and abs(float(cur) - oc) >= 0.005:
                    bits.append(f"现值 {oc:,.2f}→{float(cur):,.2f}")
                if pri is not None and abs(float(pri) - op_) >= 0.005:
                    bits.append(f"成本 {op_:,.2f}→{float(pri):,.2f}")
                lines.append(f"  {i}. {tag} {('；'.join(bits)) or '金额不变'}")
        if ignored:
            lines.append("没出现在这次数据里（保持不动）：" +
                         "、".join(f"{r['fund_name'] or r['fund_code']}({r['fund_code']})"
                                  for r in ignored[:6]))
        ready = [n for n in convs if n["amount"] > 0.005]
        if convs:
            lines.append("🔄 在途转换（这类先不直接改，免得把「搬钱」当成盈亏）：")
            for n in convs:
                lines.append(f"   #{n['cid']} {n['src_name'] or n['src_code']}({n['src_code']})"
                             f" → {n['tgt_name'] or n['tgt_code']}({n['tgt_code']})")
                shot = ("截图里已没有这只" if n["ocr_src"] is None
                        else f"截图 ¥{n['ocr_src']:,.2f}")
                if n["amount"] > 0.005:
                    lines.append(f"      台账源现值 ¥{n['full']:,.2f}，{shot}"
                                 f" → App 已执行，回「结算转换」按 ¥{n['amount']:,.2f} 立即入账")
                else:
                    lines.append(f"      台账源现值 ¥{n['full']:,.2f}，{shot}"
                                 f" → 差额只是净值涨跌，转换仍按 {n['out_date']} 自动结算，不用管")
        if ready:
            lines.append("回「确认」只改上面这些；回「结算转换」连在途转换一起按截图入账；「取消」放弃。")
        else:
            lines.append("回「确认」写入台账，「取消」放弃；也可以继续说一句重新生成方案。")
        return "\n".join(lines)

    def _settle_recon_convs(self, member: str, convs: list[dict]) -> list[str]:
        """对账时按截图金额立即结算在途转换（App 已执行、台账还在等转出日的情形）。"""
        out: list[str] = []
        for n in convs:
            if n["amount"] <= 0.005:
                continue
            cur = next((x for x in self._storage.active_conversions(member)
                        if x["id"] == n["cid"]), None)
            if not cur:
                continue
            msg = self._settle_one(cur, amount=n["amount"])
            if not msg:
                continue
            out.append(msg)
            # 转入基金：截图金额已包含刚到账的钱，把「自己那部分」对齐，批次另算
            if n.get("ocr_tgt") is not None:
                own = round(float(n["ocr_tgt"]) - n["amount"], 2)
                if own >= 0:
                    self._storage.set_values(member, n["tgt_code"], current=own)
        return out

    def _apply_recon(self, member: str, ops: list[dict], stamp: str,
                     from_image: bool) -> str:
        changed, skipped = [], 0
        up = ""  # 对账校准只写本金/现值，不标净值日（否则会骗过当晚更新、打乱在途结算）
        for o in ops:
            try:
                if o["kind"] == "delete":
                    n = self._storage.delete_record(member, o["code"])
                    if n:
                        changed.append(f"🗑 已删除 {o['name']}({o['code']})")
                    else:
                        skipped += 1
                elif o["kind"] == "insert":
                    cur = float(o.get("current") or 0.0)
                    pri = float(o.get("principal") or 0.0)
                    self._storage.upsert_full(member, o["code"], o["name"], pri, cur)
                    self._storage.add_trade_log(member, o["code"], o["name"], "校准", cur)
                    changed.append(f"🆕 新增 {o['name']}({o['code']}) 现值 ¥{cur:,.2f}")
                else:
                    rec = self._storage.get_fund(member, o["code"])
                    if not rec:
                        skipped += 1
                        continue
                    self._storage.set_values(member, o["code"],
                                             principal=o.get("principal"),
                                             current=o.get("current"), preserve=True)
                    self._storage.add_trade_log(member, o["code"], o["name"], "校准",
                                                float(o.get("current") or 0.0))
                    bits = []
                    if (o.get("old_current") is not None and o.get("current") is not None
                            and abs(float(o["current"]) - float(o["old_current"])) >= 0.005):
                        bits.append(f"现值 ¥{float(o['current']):,.2f}")
                    if (o.get("old_principal") is not None and o.get("principal") is not None
                            and abs(float(o["principal"]) - float(o["old_principal"])) >= 0.005):
                        bits.append(f"成本 ¥{float(o['principal']):,.2f}")
                    changed.append(f"{o['name']}({o['code']}) → {('，'.join(bits)) or '已更新'}")
            except Exception as e:
                logger.warning(f"[羊村基金涨跌榜] 对账写入失败 {o.get('code')}: {e}")
                skipped += 1
        head = f"✅ 对账已写入 {len(changed)} 条" + (f"（{skipped} 条跳过）" if skipped else "")
        tail = ["　榜上金额已同步刷新。"]
        return "\n".join([head] + ["　" + c for c in changed[:12]] + tail)

    async def _handle_recon_reply(self, event, umo: str, sender: str, text: str,
                                  has_img: bool) -> str:
        st = self._recon_pending.get((umo, sender))
        if not st:
            return "对账状态已失效，重新发「对账」+ 截图。"
        low = re.sub(r"[\s!！。.,，~～]", "", text.lower())
        if st.get("kind") == "convert_order":
            if low in ("确认", "确定", "登记", "应用", "写入", "ok", "okay", "y", "yes", "对"):
                return self._commit_order_conversion(st, umo, sender)
            if low in ("取消", "算了", "放弃", "不用了", "no", "n"):
                self._recon_pending.pop((umo, sender), None)
                return "已取消，没登记这笔转换。"
            if has_img:
                self._recon_pending.pop((umo, sender), None)
                return await self._recon_start(event, umo, sender, text, has_img)
            return "回「确认」登记这笔在途转换，「取消」放弃。"
        if low in ("取消", "算了", "放弃", "不用了", "no", "n"):
            self._recon_pending.pop((umo, sender), None)
            return "已取消，台账没动。"
        if low in ("确认", "确定", "应用", "写入", "执行", "ok", "okay", "y", "yes", "对"):
            ops, member = st["ops"], st["member"]
            self._recon_pending.pop((umo, sender), None)
            res = self._apply_recon(member, ops, st.get("stamp") or "",
                                    st.get("source") == "截图")
            ready = [n for n in (st.get("convs") or []) if n["amount"] > 0.005]
            if ready:
                res += (f"\n（另有 {len(ready)} 笔在途转换没动，"
                        f"回「结算转换」按截图金额入账）")
            return res
        if low in ("结算转换", "转换已结算", "结算在途", "转换入账"):
            member = st["member"]
            self._recon_pending.pop((umo, sender), None)
            out = self._settle_recon_convs(member, st.get("convs") or [])
            body = (self._apply_recon(member, st["ops"], st.get("stamp") or "",
                                      st.get("source") == "截图")
                    if st["ops"] else "✅ 其他基金无需改动")
            head = ("✅ 已按截图结算在途转换" if out
                    else "没有可结算的在途转换（可能 App 还没执行）")
            return "\n".join([head] + ["　" + x.splitlines()[0] for x in out] + [body])
        if has_img:
            self._recon_pending.pop((umo, sender), None)
            return await self._recon_start(event, umo, sender, text, has_img)
        recs = [r for r in self._storage.all_records()
                if r["member"] == st["member"] or st["member"] in r["member"]]
        ops = await _llm_parse_recon_ops(text, recs)
        if not ops:
            if not st.get("ops"):
                return ("还是没提取到内容。发持仓截图，或说「对账 016702 现值 205」；"
                        "不弄了回「取消」。")
            return "没听懂这条。回「确认」写入，「取消」放弃，或直接说「016702 现值 205」。"
        st["ops"], st["source"], st["convs"] = ops, "指令", []
        self._state_ts.setdefault((umo, sender), {})["recon"] = time.time()
        return self._render_recon_plan(st["member"], ops, "指令", [])

    # ---------- 导入数据 ----------

    async def _import_from_image(self, event, umo: str, sender: str) -> str:
        img_comp = None
        for comp in event.get_messages():
            if isinstance(comp, Image):
                img_comp = comp
                break
        if img_comp is None:
            return "导入数据需要附上一张持仓/理财截图，请把截图一起发来~"
        path = await _resolve_image_async(img_comp)
        if path is None:
            return "获取不到截图内容，请重发一张图片~"
        try:
            records, date, page_type = await _ocr_image(path)
        except Exception as e:
            logger.error(f"[羊村基金涨跌榜] 图像识别失败: {e}")
            return f"图像识别失败：{e}"
        finally:
            try:
                if path and os.path.exists(path) and not str(path).startswith(
                    str(getattr(img_comp, "file", ""))):
                    os.remove(path)
            except OSError:
                pass
        if not records:
            if self.cfg.get("ocr_strict_page", True) and page_type and page_type not in ("持仓页", "交易明细"):
                return "这张图不像基金持仓/交易截图，请发送持仓页或交易明细截图。"
            return "识别不到有效的基金数据，请确认截图是清晰的持仓表格（基金/金额）。"

        # 成员名固定取发送者的群昵称
        sender_name = _norm_member((event.get_sender_name() or "").strip())
        if not sender_name:
            sender_name = f"成员_{sender}"
        for rec in records:
            rec["member"] = sender_name

        # 日期自动取当天
        date = time.strftime("%Y-%m-%d")

        # 缺基金代码的记录，按名称自动补全代码（保证能自动更新涨跌）
        try:
            await self._fill_codes(records)
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 名称补代码失败: {e}")

        # ---- 份额确认检查（日期自动取当天，不再要求确认）----
        missing = [r["member"] for r in records if r["amount"] <= 0]
        need = []
        if missing:
            need.append("以下份额/金额不明确：" + "、".join(missing))
        if need:
            # 先保存为待确认(confirmed=0)，不参与每日更新，确认后才转正
            # 已是待确认的记录保留原值不替换
            existing_pending = {(r["member"], r["fund_code"])
                                for r in self._storage.pending_records()}
            async with self._storage._lock:
                for rec in records:
                    if (rec["member"], rec["fund_code"]) in existing_pending:
                        continue
                    self._storage.upsert_record(
                        rec["member"], rec["fund_code"], rec["fund_name"],
                        rec["amount"], confirmed=0)
            self._pending[(umo, sender)] = {"records": records, "date": date}
            self._state_ts.setdefault((umo, sender), {})["pending"] = time.time()
            msg = "⚠️ 数据已暂存为【待确认】（日期已记今天，确认前不参与每日更新），还需确认：\n" \
                + "\n".join("• " + x for x in need)
            msg += "\n份额没定可以先不管。份额确认后，你可以：\n"
            msg += "① 直接回复各基金金额，例如「招商白酒 10000，华夏成长 5000」立即转正；\n"
            msg += "② 或回复确认时间预约自动转正，例如「周四确认份额」，到点我会自动转正并更新。"
            return msg

        existing_pending = {(r["member"], r["fund_code"])
                            for r in self._storage.pending_records()}
        skipped = 0
        async with self._storage._lock:
            for rec in records:
                if (rec["member"], rec["fund_code"]) in existing_pending:
                    skipped += 1
                    continue
                self._storage.upsert_record(
                    rec["member"], rec["fund_code"], rec["fund_name"],
                    rec["amount"], confirmed=1)
        no_code = sum(1 for r in records if not r["fund_code"])
        msg = f"✅ 已导入 {len(records) - skipped} 条基金数据（日期：{date}）。时间与份额已确认。"
        if skipped:
            msg += f"\n⏳ {skipped} 条仍为待确认（保留预约/待确认状态，未替换）。"
        if no_code:
            msg += f"\n⚠️ {no_code} 条未识别到基金代码，这类基金将无法自动更新涨跌，建议导入含代码的截图。"
        msg += "\n回复「涨跌榜」可查看当前数据。"
        return msg

    async def _handle_confirm(self, event, umo: str, sender: str) -> str:
        pending = self._pending.pop((umo, sender), None)
        if not pending:
            return "没有待确认的导入记录，可重新发送「导入数据」+截图。"
        records = list(pending["records"])
        date = pending.get("date") or ""
        # 若回复携带图片 → 重新识别
        img_comp = None
        for comp in event.get_messages():
            if isinstance(comp, Image):
                img_comp = comp
                break
        if img_comp is not None:
            path = _resolve_image_file(img_comp)
            if path is None:
                return "获取不到你补发的截图，请用文字回复各成员份额/金额和日期。"
            try:
                new_records, new_date, _ = await _ocr_image(path)
                if new_records:
                    await self._fill_codes(new_records)
                    records, date = new_records, new_date or date
            except Exception as e:
                return f"重新识别失败：{e}"
            finally:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
        else:
            text = (event.message_str or "").strip()
            # 统一用大模型一次理解：是否含预约/确认时间 + 各基金金额 + 数据日期
            parsed = await _llm_parse_confirm_v2(text)
            dt = parsed.get("confirm_time")
            if dt:
                member = records[0]["member"]
                for r in records:
                    self._confirm_schedule[(member, r["fund_code"])] = dt
                    self._storage.save_schedule(member, r["fund_code"], dt.strftime("%Y-%m-%d %H:%M"))
                scheduled = True
            else:
                scheduled = False
            if parsed.get("date"):
                date = parsed["date"]
            for f in parsed.get("funds", []):
                fname = f.get("fund_name", "")
                for r in records:
                    if (fname and r["fund_name"] == fname) or (fname and fname == r["fund_code"]):
                        r["amount"] = f["amount"]
                        break
            if scheduled:
                member = records[0]["member"]
                return (f"⏰ 已预约：{member} 的待确认数据将在 "
                        f"{dt.strftime('%Y-%m-%d %H:%M')} 自动转正并更新，到点无需再操作。"
                        f"\n预约已保存，重启不丢失。如届时份额有变，可发「导入数据」+新截图覆盖。")
        if not records:
            return "未能确认到有效数据。"
        async with self._storage._lock:
            for r in records:
                self._storage.upsert_record(
                    r["member"], r["fund_code"], r["fund_name"],
                    r["amount"], confirmed=1)
        no_code = sum(1 for r in records if not r["fund_code"])
        msg = f"✅ 已确认并转正 {len(records)} 条基金数据（日期：{date or '未提供'}），将纳入每日更新。"
        if no_code:
            msg += f"\n⚠️ {no_code} 条未识别到基金代码，将无法自动更新涨跌。"
        msg += "\n回复「涨跌榜」查看。"
        return msg

    def _at_other_name(self, event: AstrMessageEvent) -> str | None:
        """取被 @ 的群成员昵称（非机器人）。"""
        try:
            self_id = str(event.get_self_id())
        except Exception:
            return None
        for comp in event.get_messages():
            if isinstance(comp, At):
                qq = str(getattr(comp, "qq", ""))
                if qq and qq != self_id and qq != "all":
                    name = str(getattr(comp, "name", "") or "").strip()
                    if name:
                        return name
        return None

    def _render_select_image(self, member: str, mode: str, recs: list) -> str | None:
        """把『记录选择列表』渲染成 PNG（预约确认/删除/修改/买入/基金情况）。
        风格与 update/board 图一致：浅蓝渐变 + 深蓝标题栏 + 成员头像 + 记录卡片。
        mode ∈ {schedule, delete, modify, buy, funds}。失败返回 None。"""
        from PIL import Image, ImageDraw, ImageFont
        font_path = self._find_font()
        if not font_path:
            return None
        emoji_path = (r"C:\Windows\Fonts\seguiemj.ttf"
                      if os.path.exists(r"C:\Windows\Fonts\seguiemj.ttf") else font_path)

        def vgrad(dr, x0, y0, x1, y1, c1, c2):
            h = y1 - y0
            for i in range(h):
                t = i / max(h - 1, 1)
                dr.line([(x0, y0 + i), (x1, y0 + i)], fill=(
                    int(c1[0] + (c2[0] - c1[0]) * t),
                    int(c1[1] + (c2[1] - c1[1]) * t),
                    int(c1[2] + (c2[2] - c1[2]) * t)))

        meta = {
            "schedule": ("📋", "预约确认 · 选择记录",
                         "回复数字（空格分隔）选择要预约的条目，如「1 2 3」；也可直接附时间，如「1 2 3 周四确认」。"),
            "delete": ("🗑️", "删除基金 · 选择记录", "回复数字（空格分隔）删除对应条目，如「1 2」。"),
            "modify": ("✏️", "修改数据 · 选择记录",
                       "回复「编号 新金额」改本金，如「1 15000」；「编号 确认/待确认」切换确认状态（调试用）。"),
            "buy": ("💹", "买入基金 · 选择持仓",
                    "回复「编号 金额」追加，如「1 3000」；或直接输入新基金名 + 金额。"),
            "sell": ("📉", "卖出基金 · 选择持仓",
                     "回复「编号 金额」卖出对应持仓的已确认份额，如「1 2000」；"
                     "回复「编号 全部」清空该条已确认本金。待确认份额不受影响。"),
            "funds": ("📂", "基金情况 · 持仓明细", "数据来自本地台账，实时展示。"),
            "convert": ("🔄", "基金转换 · 选择转出",
                        "回复「编号」整只转出，或「编号 金额」/「编号 百分比」部分转出，"
                        "如「2」「2 100」「2 50%」；转出日之前它继续跟净值，"
                        "到那天晚自动结算并挂账到转入基金。回复「取消」退出。"),
        }
        emo, title, foot = meta.get(mode, meta["funds"])

        title_f = ImageFont.truetype(font_path, 46)
        sub_f = ImageFont.truetype(font_path, 21)
        name_f = ImageFont.truetype(font_path, 32)
        fund_f = ImageFont.truetype(font_path, 25)
        small_f = ImageFont.truetype(font_path, 21)
        num_f = ImageFont.truetype(font_path, 27)
        emoji_f = ImageFont.truetype(emoji_path, 42)
        up = "#D93B34"; down = "#0E9F6E"; flat = "#8A90A0"

        W, pad = 960, 56
        title_h = 140; foot_h = 100
        card_h = 112; gap = 14

        def _wrap(txt, font, max_w):
            segs = re.split(r"(?<=[；;，,。])", txt)
            lines, cur = [], ""
            for s in segs:
                trial = cur + s
                if font.getlength(trial) > max_w and cur:
                    lines.append(cur); cur = s
                else:
                    cur = trial
            if cur:
                lines.append(cur)
            return lines

        foot_lines = _wrap(foot, small_f, W - 2 * pad)
        foot_extra = max(len(foot_lines) - 1, 0) * 34
        foot_region = foot_h + foot_extra
        head_h = 86  # 头像/成员/副标题区（位于标题栏下方，卡片从此高度开始）
        H = (title_h + head_h + foot_region + len(recs) * card_h + max(len(recs) - 1, 0) * gap)
        img = Image.new("RGB", (W, H), "#FFFFFF"); d = ImageDraw.Draw(img)
        vgrad(d, 0, 0, W, H, (240, 244, 255), (252, 253, 255))
        bar = Image.new("RGBA", (W, title_h)); db = ImageDraw.Draw(bar)
        vgrad(db, 0, 0, W, title_h, (26, 38, 96), (91, 71, 168))
        bmask = Image.new("L", (W, title_h), 0)
        ImageDraw.Draw(bmask).rounded_rectangle(
            [0, 0, W - 1, title_h - 1], radius=22,
            corners=(False, False, True, True), fill=255)
        bar.putalpha(bmask); img.paste(bar, (0, 0), bar)
        d.text((pad, 30), title, font=title_f, fill="#FFFFFF")
        d.text((pad, 88), f"{member} · 更新于 " + time.strftime("%Y-%m-%d %H:%M"),
               font=sub_f, fill="#D9DFF6")
        d.text((W - pad - 64, 30), emo, font=emoji_f, fill="#F5B301")

        avatar_d = 52
        ax = pad + 8; ay = title_h + 16
        avatar = self._load_avatar(member, avatar_d)
        if avatar is not None:
            d.ellipse([ax - 4, ay - 4, ax + avatar_d + 4, ay + avatar_d + 4],
                      fill="#FFFFFF", outline="#E3E8F6", width=2)
            img.paste(avatar, (int(ax), int(ay)), avatar)
        else:
            d.ellipse([ax, ay, ax + avatar_d, ay + avatar_d], fill="#5B9BD5")
            first = member[0] if member else "?"
            bb = d.textbbox((0, 0), first, font=name_f)
            d.text((ax + avatar_d / 2 - (bb[2] - bb[0]) / 2,
                    ay + avatar_d / 2 - (bb[3] - bb[1]) / 2 - 2),
                   first, font=name_f, fill="#FFFFFF")
        d.text((ax + avatar_d + 14, title_h + 4), member, font=name_f, fill="#20242E")
        pend = sum(1 for r in recs if not r["confirmed"])
        pendamt = sum((r.get("pending_amount") or 0.0) for r in recs if r["confirmed"])
        sub = f"{len(recs)} 条记录" + (f" · 待确认 {pend}" if pend else "") \
            + (f" · 买入待确认 ¥{pendamt:,.0f}" if pendamt > 0 else "")
        d.text((ax + avatar_d + 16, title_h + 42), sub, font=small_f, fill="#8A90A0")

        y = title_h + 86
        for i, r in enumerate(recs, 1):
            d.rounded_rectangle([pad + 2, y + 6, W - pad + 2, y + card_h + 6],
                                radius=14, fill="#E4E9F6")
            d.rounded_rectangle([pad, y, W - pad, y + card_h],
                                radius=14, fill="#FFFFFF", outline="#E3E8F6", width=1)
            cx0 = pad + 18; cy0 = y + 16
            d.ellipse([cx0, cy0, cx0 + 44, cy0 + 44], fill="#4A5FC1")
            bb = d.textbbox((0, 0), str(i), font=num_f)
            d.text((cx0 + 22 - (bb[2] - bb[0]) / 2, cy0 + 22 - (bb[3] - bb[1]) / 2 - 1),
                   str(i), font=num_f, fill="#FFFFFF")
            tx = cx0 + 60
            name = r["fund_name"] or r["fund_code"]
            d.text((tx, y + 12), name, font=fund_f, fill="#20242E")
            d.text((tx, y + 46), r["fund_code"], font=small_f, fill="#9AA0B0")
            pa = r.get("pending_amount") or 0.0
            if mode == "sell":
                # 卖出模式：只允许卖已确认部分，标签明确显示可卖金额
                if not r["confirmed"]:
                    tag, tc, bg = "整条待确认·不可卖", "#C77700", "#FFF3DF"
                elif r["principal"] <= 0:
                    tag, tc, bg = "已确认本金为0·不可卖", "#8A90A0", "#EEF0F5"
                else:
                    tag, tc, bg = f"可卖 ¥{r['principal']:,.0f}", down, "#E3F4EC"
            elif not r["confirmed"]:
                tag, tc, bg = "待确认", "#C77700", "#FFF3DF"
            elif pa > 0:
                tag, tc, bg = f"买入待确认 ¥{pa:,.0f}", "#2E6BD6", "#E6F1FF"
            else:
                tag, tc, bg = "已确认", down, "#E3F4EC"
            tw = d.textbbox((0, 0), tag, font=small_f); cw = tw[2] - tw[0] + 22; chh = 28
            d.rounded_rectangle([W - pad - 14 - cw, y + 12, W - pad - 14, y + 12 + chh],
                                radius=chh / 2, fill=bg)
            d.text((W - pad - 14 - cw + 11, y + 12 + (chh - (tw[3] - tw[1])) / 2 - 1),
                   tag, font=small_f, fill=tc)
            chg = r["last_change"] or 0.0
            pnl = r["current"] - r["principal"]
            pnl_c = up if pnl >= 0 else down
            chg_c = up if chg > 0 else (down if chg < 0 else flat)
            pnl_s = f"{'+' if pnl >= 0 else '-'}¥{abs(pnl):,.2f}"
            d.text((tx, y + 80), f"现资金 ¥{r['current']:,.2f}", font=fund_f, fill="#5B6270")
            d.text((W - pad - 18, y + 44), f"{chg:+.2f}%", font=fund_f, fill=chg_c, anchor="ra")
            d.text((W - pad - 18, y + 76), pnl_s, font=small_f, fill=pnl_c, anchor="ra")
            y += card_h + gap

        foot_y = H - foot_region
        d.line([(pad, foot_y + 12), (W - pad, foot_y + 12)], fill="#DDE3F2", width=1)
        for li, ln in enumerate(foot_lines):
            d.text((pad, foot_y + 26 + li * 34), ln, font=small_f, fill="#6B7280")
        out = os.path.join(str(_db_path().parent), "list_tmp.png")
        img.save(out, "PNG")
        return out

    def _render_member_funds(self, member: str) -> str:
        recs = self._live(self._storage.all_records())
        mine_all = [r for r in recs if self._member_matches(r["member"], member)]
        mine = [r for r in mine_all if r["confirmed"]]
        pend = [r for r in mine_all if not r["confirmed"]]
        if not mine_all:
            return f"📂 群里暂无「{member}」的基金数据。"
        if not mine:
            return f"📂 {member} 的基金数据均待确认，确认份额后才会展示与更新（共 {len(pend)} 条）。"
        lines = [f"📂 {member} 的基金情况"]
        for r in mine:
            pnl = r["current"] - r["principal"]
            sign = "+" if pnl >= 0 else "-"
            fund_name = r["fund_name"] or r["fund_code"]
            lines.append(
                f"• {fund_name}({r['fund_code']}) 现资金¥{r['current']:,.2f} "
                f"{r['last_change']:+.2f}% 盈亏{sign}¥{abs(pnl):,.2f}"
            )
        if pend:
            lines.append(f"⏳ 另有 {len(pend)} 条待确认数据，确认份额后自动纳入。")
        return "\n".join(lines)

    # ---------- 消息发送与定时撤回（学习爬楼插件：QQ官方可撤回 bot 自己的消息） ----------

    @staticmethod
    def _is_zeroed(r: dict) -> bool:
        """已确认但本金/现值都为0 = 转换结算后已转出，展示时跳过。"""
        return (r.get("confirmed")
                and (r.get("principal") or 0) <= 0.005
                and (r.get("current") or 0) <= 0.005)

    @classmethod
    def _live(cls, records: list) -> list:
        """过滤掉已清零的转出基金，保留待确认/有余额的记录。"""
        return [r for r in records if not cls._is_zeroed(r)]

    @staticmethod
    def _norm_member(name: str) -> str:
        """去掉成员名里的 emoji/空白/非字母数字，用于昵称与台账名的宽容匹配。"""
        if not name:
            return ""
        return re.sub(r"[\W_]+", "", name).lower()

    @staticmethod
    def _member_matches(record_member: str, name: str) -> bool:
        """成员名匹配：容忍 QQ 昵称带 emoji（如 成员昵称 匹配台账 成员昵称）。"""
        a = Main._norm_member(record_member)
        b = Main._norm_member(name)
        if not a or not b:
            return record_member == name
        return a == b or a in b or b in a

    @staticmethod
    def _group_openid_from_umo(umo: str) -> str:
        """从 unified_msg_origin 提取群 openid。格式: 平台:GroupMessage:<group_openid>"""
        if "GroupMessage:" in umo:
            return umo.split("GroupMessage:")[-1].strip()
        return umo

    @staticmethod
    def _get_bot_api(event):
        bot = getattr(event, "bot", None)
        return getattr(bot, "api", None)

    @staticmethod
    def _extract_mid(ret) -> str | None:
        try:
            mid = ret.get("id") if isinstance(ret, dict) else getattr(ret, "id", None)
            return str(mid) if mid else None
        except Exception:
            return None

    async def _send_prompt(self, event, group_id: str, text: str,
                           recall_delay: int = 60) -> str | None:
        """主动发送一条提示文本（走 QQ 官方接口，可拿 msg_id 供定时撤回）。
        群聊走 /v2/groups，私聊(C2C)走 /v2/users，由 self._send_mode 决定。
        返回 msg_id；发送失败返回 None。"""
        api = self._get_bot_api(event)
        if api is None:
            logger.warning(f"[羊村基金涨跌榜] _send_prompt: bot api 为 None, 无法发送提示")
            return None
        payload: dict = {"content": text, "msg_type": 0}
        mode = getattr(self, "_send_mode", "group")
        # 注意：不向 payload 传 msg_id。QQ 官方消息接口把 msg_id 用作幂等去重键，
        # 同一条源消息触发插件多条提示时会因重复 msg_id 被 QQ 判为「消息被去重」而发送失败。
        try:
            if mode == "private":
                ret = await api.post_c2c_message(openid=group_id, **payload)
            else:
                ret = await api.post_group_message(group_openid=group_id, **payload)
        except Exception as e:
            logger.error(f"[羊村基金涨跌榜] 提示发送失败: {e}")
            return None
        mid = self._extract_mid(ret)
        if mid:
            if recall_delay and recall_delay > 0:
                asyncio.create_task(
                    self._recall_after(api, group_id, str(mid), recall_delay, mode))
                logger.info(f"[羊村基金涨跌榜] 提示已发送并调度定时撤回 mid={mid} mode={mode}")
            else:
                logger.info(f"[羊村基金涨跌榜] 提示已发送且不自动撤回 mid={mid}")
        else:
            logger.warning(f"[羊村基金涨跌榜] 提示已发送但未取到 mid: ret={ret}")
        return mid

    async def _recall_after(self, api, group_id: str, mid: str, delay: int,
                            mode: str | None = None) -> None:
        """延时 delay 秒后撤回一条机器人消息（由发送时单独调度，不依赖后台循环）。
        mode: 'group' 走 /v2/groups，'private' 走 /v2/users；未指定时读 self._send_mode。"""
        await asyncio.sleep(delay)
        try:
            from botpy.http import Route  # 延迟导入，避免非 QQOfficial 环境报错
            if (mode or getattr(self, "_send_mode", "group")) == "private":
                route = Route(
                    "DELETE",
                    "/v2/users/{openid}/messages/{message_id}",
                    openid=group_id,
                    message_id=mid,
                )
            else:
                route = Route(
                    "DELETE",
                    "/v2/groups/{group_openid}/messages/{message_id}",
                    group_openid=group_id,
                    message_id=mid,
                )
            await api._http.request(route)
            logger.info(f"[羊村基金涨跌榜] 已定时撤回提示 mid={mid} mode={mode or getattr(self, '_send_mode', 'group')}")
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 定时撤回提示失败 {group_id}/{mid}: {e}")

    # ---------- 事件 ----------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        logger.debug(f"[羊村基金涨跌榜] on_group_message触发 text={(event.message_str or '')!r}")
        produced = False
        async for _r in self._dispatch(event, "group"):
            produced = True
            yield _r
        # 只要本插件已经产出了结果（图片/文字），就必须终止事件，不能再让 LLM
        # 拿着旧上下文补一句“查不了/工具报错”，否则就是典型的「已读乱回」。
        # 普通闲聊 _dispatch 不产出任何结果，produced=False，照常放行给 LLM。
        if produced:
            event.stop_event()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        produced = False
        async for _r in self._dispatch(event, "private"):
            produced = True
            yield _r
        if produced:
            event.stop_event()

    @filter.regex(r"^\s*\d{1,2}\s*[号只个]?\s*$", priority=1)
    async def on_holdings_digit(self, event: AstrMessageEvent):
        """裸数字（「6」/「6号」/@我 4）也走插件自己的状态机。
        框架只会把这种纯数字消息交给本 regex handler（普通 on_group_message 收不到），
        所以凡是多轮状态（选基、转换、对账、买入、卖出、修改、预约、导入确认）
        都要在这里转发一次，否则「回复编号」会被静默吞掉。"""
        umo = event.unified_msg_origin or ""
        sender = str(event.get_sender_id() or "")
        states = (self._holdings_select, self._convert_select, self._recon_pending,
                  self._buy_select, self._sell_select, self._edit_select,
                  self._schedule_select, self._pending, self._order_select)
        if not any((umo, sender) in s for s in states):
            return
        mode = "group" if not event.is_private_chat() else "private"
        try:
            async for _r in self._dispatch(event, mode):
                yield _r
        finally:
            # 标记已处理，防止普通 on_group_message 再走一遍造成重复回复
            event.set_extra("_holdings_digit_done", True)

    async def _dispatch(self, event: AstrMessageEvent, mode: str):
        """统一的消息分发入口。mode='group' 走群聊（需@机器人、支持@他人查基金）；
        mode='private' 走私聊（无需@、QQ昵称即群名片，默认操作本人）。"""
        if event.get_extra("_holdings_digit_done"):
            return  # 纯数字已由 on_holdings_digit 处理过，避免重复消费
        umo = event.unified_msg_origin or ""
        sender = str(event.get_sender_id() or "")
        if mode == "private":
            # 私聊：目标即私聊用户 openid（供 post_c2c_message / 私聊撤回）
            group_id = sender
            self._send_mode = "private"
        else:
            group_id = self._group_openid_from_umo(umo)
            self._send_mode = "group"
        if self._bot_api is None:
            self._bot_api = self._get_bot_api(event)
            logger.info(f"[羊村基金涨跌榜] 缓存 bot api: {self._bot_api is not None} umo={umo} mode={mode}")
        text = (event.message_str or "").strip()
        # 群聊主动指令/关键词入口必须 @ 机器人才响应；私聊直接放行。
        # 多轮状态机应答（本人已在选基/卖出/转换/对账等流程里）按 (umo,sender) 匹配，
        # 不受此门禁限制，旁人没有状态自然进不来。
        mention = self._is_mention_bot(event)
        _group_cmd = mode == "private" or mention
        # 带图消息不参与基金多轮状态机：基金确认/预约/删除回复都是纯文字，带图必是
        # 识图等其它用途，直接清除该用户残留状态并放行，避免拦截识图插件。
        has_img = any(isinstance(c, Image) for c in event.get_messages())
        # 2.84) 买/卖截图确认方案：回「确认」落库，「取消」放弃
        if (umo, sender) in self._order_select:
            st_o = self._order_select.get((umo, sender))
            low_o = re.sub(r"[\s!！。.,，~～]", "", text.lower())
            if low_o in ("取消", "算了", "放弃", "不用了", "no", "n"):
                self._order_select.pop((umo, sender), None)
                self._state_ts.pop((umo, sender), None)
                res = "已取消，没登记这笔交易。"
                await self._send_prompt(event, group_id, res)
                event.stop_event()
                return
            if low_o in ("确认", "确定", "登记", "应用", "写入", "执行",
                         "ok", "okay", "y", "yes", "对"):
                self._order_select.pop((umo, sender), None)
                self._state_ts.pop((umo, sender), None)
                res = self._commit_order(st_o)
                await self._send_prompt(event, group_id, res)
                event.stop_event()
                return
            if has_img:
                self._order_select.pop((umo, sender), None)
                res = await self._propose_order_shot(event, umo, sender)
                if res is not None:
                    await self._send_prompt(event, group_id, res)
                    event.stop_event()
                    return
            else:
                await self._send_prompt(event, group_id,
                                        "回「确认」登记这笔交易，「取消」放弃。")
                event.stop_event()
                return
        # 2.85) 对账 / 大模型改库：先于「带图清状态」，截图也能进；先出差异清单、回「确认」才写库
        if (umo, sender) in self._recon_pending:
            res = await self._handle_recon_reply(event, umo, sender, text, has_img)
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        _norm = (text or "").replace("帐", "账").replace("賬", "账")
        if _group_cmd and (("对账" in _norm) or ("校准" in text) or ("核对" in text) \
                or text.strip() in ("AI修改", "智能修改", "改库")):
            res = await self._recon_start(event, umo, sender, text, has_img)
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        # 2.86) 直接发「转换」截图也认（不用先说「对账」）：
        # 只有「裸图」或文字里提到 转换/换/份额 时才跑一次识别，别给每张图都加一次 OCR。
        _plain = re.sub(r"\[[^\]]*\]|<@!?[^>]*>|<[^>]{1,60}>", "", text or "")
        _plain = re.sub(r"\s+", "", _plain)
        # 2.86a) 买入/卖出交易截图：裸图也先识别是不是买/卖单，是就出确认方案（回确认才落库）
        if _group_cmd and has_img:
            res = await self._propose_order_shot(event, umo, sender)
            if res:
                await self._send_prompt(event, group_id, res)
                event.stop_event()
                return
        if _group_cmd and has_img and (not _plain or re.search(r"转换|换仓|转出|转入|份额|调仓", _plain)):
            res = await self._try_convert_shot(event, umo, sender)
            if res:
                await self._send_prompt(event, group_id, res)
                event.stop_event()
                return
        if has_img:
            self._pending.pop((umo, sender), None)
            self._schedule_select.pop((umo, sender), None)
            self._edit_select.pop((umo, sender), None)
            self._buy_select.pop((umo, sender), None)
            self._sell_select.pop((umo, sender), None)
            self._convert_select.pop((umo, sender), None)
            self._order_select.pop((umo, sender), None)
        # 状态机超时自动清理（5 分钟未操作视为作废），防止残留长期拦截后续消息
        sts = self._state_ts.get((umo, sender))
        if sts:
            _now = time.time()
            if "pending" in sts and _now - sts["pending"] > 300:
                self._pending.pop((umo, sender), None)
                sts.pop("pending", None)
            if "schedule" in sts and _now - sts["schedule"] > 300:
                self._schedule_select.pop((umo, sender), None)
                sts.pop("schedule", None)
            if "edit" in sts and _now - sts["edit"] > 300:
                self._edit_select.pop((umo, sender), None)
                sts.pop("edit", None)
            if "buy" in sts and _now - sts["buy"] > 300:
                self._buy_select.pop((umo, sender), None)
                sts.pop("buy", None)
            if "sell" in sts and _now - sts["sell"] > 300:
                self._sell_select.pop((umo, sender), None)
                sts.pop("sell", None)
            if "convert" in sts and _now - sts["convert"] > 300:
                self._convert_select.pop((umo, sender), None)
                sts.pop("convert", None)
            if "order" in sts and _now - sts["order"] > 300:
                self._order_select.pop((umo, sender), None)
                sts.pop("order", None)
            if "recon" in sts and _now - sts["recon"] > 600:
                self._recon_pending.pop((umo, sender), None)
                sts.pop("recon", None)
            if "buy_confirm" in sts and _now - sts["buy_confirm"] > 300:
                # 超时保留买入挂账（不自动回滚，避免误删用户已买入数据），未预约可稍后「预约确认」
                self._buy_confirm.pop((umo, sender), None)
                sts.pop("buy_confirm", None)
            if not sts:
                self._state_ts.pop((umo, sender), None)
        # 2.87) 基金重仓股选基回复优先于待确认拦截（否则数字编号会被 \d 正则抢走）
        logger.debug(f"[羊村基金涨跌榜] holdings_select检查 umo={umo} sender={sender} in_state={(umo,sender) in self._holdings_select} text={text!r}")
        if (umo, sender) in self._holdings_select:
            st_h = self._holdings_select.get((umo, sender))
            if re.search(r"取消|算了|退出|不用了", text):
                self._holdings_select.pop((umo, sender), None)
                yield event.plain_result("已取消。")
                return
            recs = (st_h or {}).get("records", [])
            # 只认「干净编号」：先剥掉 @、引用、表情等标记再匹配。
            # 否则像 <@4A8C863C…> 这种 At 里的数字会被当成编号，选中完全不相干的基金。
            _clean = re.sub(r"\[[^\]]*\]|<@!?[^>]*>|<[^>]{1,60}>", " ", text or "")
            _clean = re.sub(r"\s+", " ", _clean).strip()
            _m = re.fullmatch(r"(\d{1,2})\s*[号只个]?", _clean)
            if not _m or not (1 <= int(_m.group(1)) <= len(recs)):
                yield event.plain_result(f"回复编号 1-{len(recs)}（或「取消」）。")
                return
            r = recs[int(_m.group(1)) - 1]
            self._holdings_select.pop((umo, sender), None)
            yield event.plain_result(f"📡 正在拉【{r['fund_name'] or r['fund_code']}】的重仓股实时行情…")
            try:
                fname, holds = await asyncio.to_thread(self._fetch_fund_holdings, r["fund_code"])
                if not holds:
                    yield event.plain_result(
                        "这只基金最新季报里没有股票重仓（指数/ETF联接、债券、货币类基金通常如此），"
                        "没有可展示的重仓股行情。")
                    return
                chgs = await asyncio.to_thread(self._fetch_stock_changes, [h[0] for h in holds])
                rows = [(tk, nm, w, chgs.get(tk)) for tk, nm, w in holds]
                got = [x for x in rows if x[3] is not None]
                tot_w = sum(x[2] for x in got) or 1
                pred = (sum(x[2] * x[3] for x in got) / tot_w) if got else 0.0
                all_w = sum(x[2] for x in rows) or 1
                cover = 100.0 * sum(x[2] for x in got) / all_w
                curve, kind = await asyncio.to_thread(self._fetch_intraday_curve, holds, r["fund_code"])
                img = self._render_holdings_image(fname, rows, round(pred, 2), curve, kind, cover)
                if img:
                    yield event.image_result(img)
                    return
            except Exception as e:
                logger.error(f"[羊村基金涨跌榜] 重仓股查询失败: {e}")
                yield event.plain_result("拉取重仓股失败，稍后再试。")
            return
        # 1.5) 预约到点后，等待该成员本人回复确认结果（回复后才转正并撤回到点提示）
        mname = _norm_member((event.get_sender_name() or "").strip())
        cf_member = self._match_pending_confirm_member(sender, mname)
        _cancel_re = re.compile(r"取消|算了|退出|不用了|不弄了|不搞了|到此为止")
        if cf_member and re.search(r"\d|确认|转正|到账|份额|更新|取消|金额|算了|退出|不用了|不弄了", text):
            if _cancel_re.search(text):
                await self._cancel_pending_confirm(cf_member)
                res = (f"已取消 {cf_member} 的确认等待，数据保持待确认。\n"
                       f"可重新「预约确认」，或随时回复确认结果转正。")
            else:
                await self._send_prompt(event, group_id,
                                        f"正在按最近估值转正更新 {cf_member} 的持仓，请稍候…")
                updated = await self._confirm_and_update(cf_member)
                await self._cancel_pending_confirm(cf_member)
                res = (f"✅ 已收到 {cf_member} 的确认结果，按最近估值转正更新 {updated} 条持仓。\n"
                       f"回复「涨跌榜」查看最新数据。")
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        # 1) 有待确认的导入 → 处理导入者的确认回复
        if (umo, sender) in self._pending:
            if _cancel_re.search(text):
                self._pending.pop((umo, sender), None)
                self._state_ts.pop((umo, sender), None)
                res = "已取消本次导入确认，数据暂不保存。可重新发送「导入数据」+截图。"
            else:
                await self._send_prompt(event, group_id, "正在用大模型理解你的确认…")
                res = await self._handle_confirm(event, umo, sender)
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        # 2) 预约确认的选择流程
        if (umo, sender) in self._schedule_select:
            if _cancel_re.search(text):
                st_sel = self._schedule_select.pop((umo, sender), None)
                if st_sel:
                    await self._recall_prompt_mid(group_id, st_sel.get("prompt_mid"))
                    self._state_ts.pop((umo, sender), None)
                res = "已取消预约确认，数据保持待确认。"
            else:
                st_before = self._schedule_select.get((umo, sender))
                res = await self._handle_schedule_select(event, umo, sender)
                # 流程已结束（预约已提交）→ 撤回最初的选择提示，回归正常
                if st_before and (umo, sender) not in self._schedule_select:
                    await self._recall_prompt_mid(group_id, st_before.get("prompt_mid"))
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        # 2.5) 删除/修改数据的选择流程
        if (umo, sender) in self._edit_select:
            if _cancel_re.search(text):
                self._edit_select.pop((umo, sender), None)
                self._edit_records.pop((umo, sender), None)
                self._state_ts.pop((umo, sender), None)
                res = "已取消本次操作，数据未改动。"
            else:
                res = await self._handle_edit_select(event, umo, sender)
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        # 2.75) 买入基金的选择流程（完成后撤回最初提示）
        if (umo, sender) in self._buy_select:
            if _cancel_re.search(text):
                st_buy = self._buy_select.pop((umo, sender), None)
                if st_buy:
                    await self._recall_prompt_mid(group_id, st_buy.get("prompt_mid"))
                    self._state_ts.pop((umo, sender), None)
                res = "已取消买入，未作任何改动。"
            else:
                st_buy = self._buy_select.get((umo, sender))
                res = await self._handle_buy_select(event, umo, sender)
                if st_buy and (umo, sender) not in self._buy_select:
                    await self._recall_prompt_mid(group_id, st_buy.get("prompt_mid"))
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        # 2.8) 买入后的确认时间询问（可「取消」撤销本次买入）
        if (umo, sender) in self._buy_confirm:
            st_cf = self._buy_confirm.get((umo, sender))
            if _cancel_re.search(text):
                self._buy_confirm.pop((umo, sender), None)
                self._state_ts.pop((umo, sender), None)
                if st_cf:
                    await self._recall_prompt_mid(group_id, st_cf.get("prompt_mid"))
                    res = self._undo_buy(st_cf)
                else:
                    res = "已取消买入确认，未作改动。"
            else:
                dt = await _llm_parse_confirm_time(text)
                if dt:
                    member = st_cf["member"]; code = st_cf["code"]
                    self._confirm_schedule[(member, code)] = dt
                    self._storage.save_schedule(member, code, dt.strftime("%Y-%m-%d %H:%M"))
                    self._buy_confirm.pop((umo, sender), None)
                    self._state_ts.pop((umo, sender), None)
                    if st_cf:
                        await self._recall_prompt_mid(group_id, st_cf.get("prompt_mid"))
                    res = (f"✅ 已为 {st_cf['name']}({code}) 预约份额确认于 "
                           f"{dt.strftime('%Y-%m-%d %H:%M')} 自动转正。\n回复「涨跌榜」查看数据。")
                else:
                    res = ("没看懂确认时间，请回复如「周四15:00」「明天确认」，"
                           "或回复「取消」撤销本次买入。")
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        # 2.85) 卖出基金的选择流程（完成后撤回最初提示）
        if (umo, sender) in self._sell_select:
            if _cancel_re.search(text):
                st_sell = self._sell_select.pop((umo, sender), None)
                if st_sell:
                    await self._recall_prompt_mid(group_id, st_sell.get("prompt_mid"))
                    self._state_ts.pop((umo, sender), None)
                res = "已取消卖出，未作任何改动。"
            else:
                st_sell = self._sell_select.get((umo, sender))
                res = await self._handle_sell_select(event, umo, sender)
                if st_sell and (umo, sender) not in self._sell_select:
                    await self._recall_prompt_mid(group_id, st_sell.get("prompt_mid"))
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        # 2.86) 基金转换流程（转出→转入→确认时间，三阶段）
        if (umo, sender) in self._convert_select:
            st_cv = self._convert_select.get((umo, sender))
            if _cancel_re.search(text):
                # 仅在还没扣款（source 阶段）才整体作废；已扣款则不回滚，提示用户自行撤销转入挂账
                cancelled_stage = st_cv.get("stage") if st_cv else None
                self._convert_select.pop((umo, sender), None)
                self._state_ts.pop((umo, sender), None)
                if st_cv:
                    await self._recall_prompt_mid(group_id, st_cv.get("prompt_mid"))
                # 在途模式下全程不扣款，退出即作废
                res = "已取消基金转换，未作任何改动。"
            else:
                stage = (st_cv or {}).get("stage")
                if stage == "target":
                    res = await self._handle_convert_target(event, umo, sender)
                elif stage in ("review", "outdate", "confirmtime"):
                    res = await self._handle_convert_review(event, umo, sender)
                else:
                    res = await self._handle_convert_source(event, umo, sender)
                if st_cv and (umo, sender) not in self._convert_select:
                    await self._recall_prompt_mid(group_id, st_cv.get("prompt_mid"))
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        # 3) 基金情况：群聊支持 @他人 导出该成员；私聊默认查自己
        if _group_cmd and "基金情况" in text:
            if mode == "group":
                target_name = self._at_other_name(event)
            else:
                target_name = mname
            if target_name:
                recs = [r for r in self._live(self._storage.all_records())
                        if self._member_matches(r["member"], target_name)]
                img = self._render_select_image(target_name, "funds", recs) if recs else None
                if img:
                    yield event.image_result(img)
                else:
                    yield event.plain_result(self._render_member_funds(target_name))
                return
        # 3.5) 「基金持仓 / 基金持股」：直接走本地台账，别交给 LLM 猜。
        #      QQ 官方的 @ 组件偶尔取不到 self_id，若放在下面的 @ 门禁之后会被静默吞掉，
        #      用户看到的就是「已读不回」，随后 LLM 又可能拿旧上下文乱答。
        if _group_cmd and (("基金持仓" in text) or ("基金持股" in text) or ("重仓股" in text) or ("重仓前十" in text)):
            member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
            recs = [r for r in self._live(self._storage.all_records())
                    if (self._member_matches(r["member"], member)) and r["confirmed"]]
            if not recs:
                await self._send_prompt(event, group_id,
                                        f"📋 {member} 名下没有基金记录。")
                event.stop_event()
                return
            self._holdings_select[(umo, sender)] = {"member": member, "records": recs}
            logger.info(f"[羊村基金涨跌榜] 已设置持仓选基状态 umo={umo} sender={sender} 基金数={len(recs)}")
            picker = self._render_fund_picker_image(member, recs)
            if picker:
                yield event.image_result(picker)
                return
            await self._send_prompt(event, group_id,
                                    "回复编号选一只基金看重仓股实时涨跌。")
            event.stop_event()
            return
        # 4) 指令：群聊需@机器人，私聊直接放行
        if mode == "group" and not self._is_mention_bot(event):
            return
        if not text:
            return
        if "取消预约" in text or "取消确认" in text or "取消预约确认" in text:
            member = _norm_member((event.get_sender_name() or "").strip())
            # 若正处预约确认选择流程 → 撤回选择提示并清除状态，回归正常聊天
            if (umo, sender) in self._schedule_select:
                st_sel = self._schedule_select.pop((umo, sender), None)
                if st_sel:
                    await self._recall_prompt_mid(group_id, st_sel.get("prompt_mid"))
                    self._state_ts.pop((umo, sender), None)
            res = await self._cancel_reserve(member)
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        if "预约确认" in text:
            member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
            recs = [r for r in self._live(self._storage.all_records())
                    if self._member_matches(r["member"], member)]
            if not recs:
                await self._send_prompt(event, group_id,
                                        f"📋 {member} 名下暂无记录，请先「导入数据」+截图。")
                event.stop_event()
                return
            self._schedule_select[(umo, sender)] = {
                "member": member, "stage": "choose", "records": recs, "selected": [],
                "prompt_mid": None,
            }
            self._state_ts.setdefault((umo, sender), {})["schedule"] = time.time()
            img = self._render_select_image(member, "schedule", recs)
            if img:
                yield event.image_result(img)
            else:
                mid = await self._send_prompt(event, group_id,
                                              await self._start_schedule(event, umo, sender),
                                              recall_delay=0)
                if mid and (umo, sender) in self._schedule_select:
                    self._schedule_select[(umo, sender)]["prompt_mid"] = mid
            event.stop_event()
            return
        if "删除基金" in text or "删基金" in text:
            _mode = "delete"
        elif "修改数据" in text or "改数据" in text:
            _mode = "modify"
        else:
            _mode = None
        if _mode:
            member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
            recs = [r for r in self._storage.all_records()
                    if self._member_matches(r["member"], member)]
            if not recs:
                await self._send_prompt(event, group_id,
                                        f"📋 {member} 名下暂无记录，请先「导入数据」+截图。")
                event.stop_event()
                return
            img = self._render_select_image(member, _mode, recs)
            if img:
                self._edit_select[(umo, sender)] = _mode
                self._edit_records[(umo, sender)] = recs
                self._state_ts.setdefault((umo, sender), {})["edit"] = time.time()
                yield event.image_result(img)
            else:
                res = self._start_edit(event, umo, sender, _mode)
                await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        if "买入基金" in text or "买基金" in text or "加仓" in text or "申购" in text:
            member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
            recs = [r for r in self._live(self._storage.all_records())
                    if self._member_matches(r["member"], member)]
            self._buy_select[(umo, sender)] = {"member": member, "records": recs, "prompt_mid": None}
            self._state_ts.setdefault((umo, sender), {})["buy"] = time.time()
            img = self._render_select_image(member, "buy", recs) if recs else None
            if img:
                yield event.image_result(img)
            else:
                mid = await self._send_prompt(event, group_id,
                                              await self._start_buy(event, umo, sender),
                                              recall_delay=0)
                if mid and (umo, sender) in self._buy_select:
                    self._buy_select[(umo, sender)]["prompt_mid"] = mid
            event.stop_event()
            return
        if "卖出基金" in text or "卖基金" in text or "赎回" in text or "减仓" in text:
            member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
            recs = [r for r in self._live(self._storage.all_records())
                    if self._member_matches(r["member"], member)]
            if not recs:
                await self._send_prompt(event, group_id,
                                        f"📋 {member} 名下暂无持仓记录，请先「导入数据」+截图。")
                event.stop_event()
                return
            self._sell_select[(umo, sender)] = {"member": member, "records": recs, "prompt_mid": None}
            self._state_ts.setdefault((umo, sender), {})["sell"] = time.time()
            img = self._render_select_image(member, "sell", recs)
            if img:
                yield event.image_result(img)
            else:
                mid = await self._send_prompt(event, group_id,
                                              await self._start_sell(event, umo, sender),
                                              recall_delay=0)
                if mid and (umo, sender) in self._sell_select:
                    self._sell_select[(umo, sender)]["prompt_mid"] = mid
            event.stop_event()
            return
        if "取消转换" in text or "撤销转换" in text:
            member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
            res = await self._cancel_conversion(member, text)
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        m_sh = re.search(r"^(?:改)?(?:转换|转出)(?:份额|份数)\s*[:：]?\s*([\d.]+)", text)
        if m_sh:
            member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
            res = self._set_convert_shares(member, m_sh.group(1))
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        if text.strip() in ("转换状态", "转换记录", "我的转换", "在途转换"):
            member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
            await self._send_prompt(event, group_id, self._render_convert_status(member))
            event.stop_event()
            return
        if ("转换基金" in text) or ("基金转换" in text) or text.strip() == "转换":
            member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
            recs = [r for r in self._storage.all_records()
                    if self._member_matches(r["member"], member)]
            # 可以同时有多笔在途转换，唯一真正冲突的是「同一只基金被转出两次」
            # （结算时会把同一笔钱清零两次），所以只把已在转换中的转出基金剔出列表。
            active = self._storage.active_conversions(member)
            busy = {a["src_code"] for a in active}
            pool = [r for r in recs if r["confirmed"] and r["principal"] > 0]
            sellable = [r for r in pool if r["fund_code"] not in busy]
            if not sellable:
                if active:
                    await self._send_prompt(
                        event, group_id,
                        "😴 你的持仓都已经在转换里了：\n" + "\n".join(
                            f"　·【{a['src_name'] or a['src_code']}】→"
                            f"【{a['tgt_name'] or a['tgt_code']}】转出日 {a['out_date']}"
                            for a in active)
                        + "\n等它结算，或回复「取消转换」撤销（可指定编号）。")
                else:
                    await self._send_prompt(event, group_id,
                                            f"📋 {member} 名下没有已确认持仓可转出，请先「导入数据」。")
                event.stop_event()
                return
            if active:
                await self._send_prompt(
                    event, group_id,
                    f"ℹ️ 你还有 {len(active)} 笔在途转换（对应基金已从列表剔除），"
                    f"本次可选 {len(sellable)} 只。", recall_delay=0)
            self._convert_select[(umo, sender)] = {
                "member": member, "stage": "source", "records": sellable,
                "prompt_mid": None, "busy": busy}
            self._state_ts.setdefault((umo, sender), {})["convert"] = time.time()
            img = self._render_select_image(member, "convert", sellable)
            if img:
                yield event.image_result(img)
            else:
                mid = await self._send_prompt(event, group_id,
                                              await self._start_convert(event, umo, sender),
                                              recall_delay=0)
                if mid and (umo, sender) in self._convert_select:
                    self._convert_select[(umo, sender)]["prompt_mid"] = mid
            event.stop_event()
            return
        if ("盘中估值" in text) or ("盘中" in text):
            member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
            recs = [r for r in self._live(self._storage.all_records())
                    if (self._member_matches(r["member"], member)) and r["confirmed"]]
            if not recs:
                await self._send_prompt(event, group_id, f"📋 {member} 名下没有基金记录。")
                event.stop_event()
                return
            if _a_share_preopen():
                # 09:30 前 A股报价还是「昨收/0」，出图会是一排 0.00，先给盘前参考
                tip = "⏳ A股还没开盘（09:30 起连续竞价），这会儿没有盘中盘面。"
                a50 = self._fetch_a50_change()
                if a50:
                    tip += f"\n盘前参考：A50 期货 {a50[0]:+.2f}%（{a50[2]}）"
                await self._send_prompt(event, group_id, tip, recall_delay=0)
                event.stop_event()
                return
            items = []
            row = None
            tot_num = tot_den = 0.0
            phase = ""
            snap = self._load_close_snapshot() if self._is_after_close() else None
            snap_row = None
            if snap:
                for sr in snap.get("rows") or []:
                    if (sr.get("member") or "").strip() == member:
                        snap_row = sr
                        break
            if snap_row and snap_row.get("items"):
                phase = "已收盘"
                for it in snap_row["items"]:
                    items.append({"fund_code": it.get("fund_code"), "fund_name": it.get("fund_name"),
                                  "current": it.get("current") or 0.0, "est": it.get("est"),
                                  "kind": it.get("kind") or "none"})
                tot_den = sum((it["current"] or 0.0) for it in items if it["est"] is not None)
                tot_num = sum((it["current"] or 0.0) * it["est"] for it in items if it["est"] is not None)
                total = round(tot_num / tot_den, 2) if tot_den else None
                row = {"member": member,
                       "current": round(sum(it["current"] or 0.0 for it in items), 2),
                       "est": total,
                       "amount": round(tot_num / 100.0, 2) if tot_den else None,
                       "items": items}
            else:
                # 与自动推送同一取数：昨夜美股覆盖 + 在途卖出并入，保证指令图和推图一致
                try:
                    est_rows = self._collect_estimate_rows()
                except Exception as e:
                    logger.warning(f"[羊村基金涨跌榜] 指令盘中估值取数失败: {e}")
                    est_rows = []
                row = next((r for r in est_rows
                            if self._member_matches(r["member"], member)), None)
                items = row["items"] if row else []
                total = row["est"] if row else None
                if self._is_after_close():
                    phase = "已收盘"
            logger.info(f"[羊村基金涨跌榜] 盘中估值 umo={umo} sender={sender} 基金数={len(items)} "
                        f"total={total} phase={phase or '盘中'}")
            pend_total = 0.0
            try:
                pend_total = sum(float(r.get("pending_amount") or 0)
                                 for r in self._storage.all_records()
                                 if self._member_matches(r["member"], member))
            except Exception:
                pend_total = 0.0
            img = self._render_estimate_board_image(
                [row] if row else [], f"盘中估值 {member}", pend_total, "", False)
            if img:
                yield event.image_result(img)
                return
            lines = [f"📈 {member} {'已收盘估值' if phase else '盘中估值'}"]
            for it in items:
                if it["est"] is None:
                    lines.append(f"  {it['fund_name'] or it['fund_code']} —")
                else:
                    lines.append(f"  {it['fund_name'] or it['fund_code']} {it['est']:+.2f}%")
            if total is not None:
                lines.append(f"总预估 {total:+.2f}%")
            await self._send_prompt(event, group_id, "\n".join(lines))
            event.stop_event()
            return
        if "交易明细" in text or "买卖记录" in text or "交易记录" in text:
            member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
            logs = self._storage.trade_logs(member)
            if not logs:
                await self._send_prompt(event, group_id,
                                        f"📋 {member} 暂无买卖记录（买入/卖出都会留痕）。")
                event.stop_event()
                return
            lines = [f"📑 {member} 的交易明细（最近 {len(logs)} 条）"]
            for lg in logs:
                op = lg["op_type"]
                ic = "💹" if op == "买入" else "📉"
                lines.append(f"{ic} {lg['ts'][:16]} {lg['fund_name'] or lg['fund_code']} "
                             f"{op} ¥{lg['amount']:,.2f}")
            await self._send_prompt(event, group_id, "\n".join(lines), recall_delay=0)
            event.stop_event()
            return
        if "涨跌榜" in text:
            try:
                img_path = self._render_board_image()
                if img_path:
                    yield event.image_result(img_path)
                    return
            except Exception as e:
                logger.error(f"[羊村基金涨跌榜] 涨跌榜图片生成失败: {e}")
            yield event.plain_result(self._render_board())
            return
        if "导入数据" in text:
            await self._send_prompt(event, group_id, "正在识别截图中的基金数据，请稍候…")
            res = await self._import_from_image(event, umo, sender)
            await self._send_prompt(event, group_id, res)
            event.stop_event()
            return
        if "何时更新" in text or "更新分析" in text or "什么时候更新" in text:
            await self._send_prompt(event, group_id, "正在联网分析基金更新状态，请稍候…")
            try:
                img_path = await self._cmd_update_analysis(event)
                if img_path:
                    yield event.image_result(img_path)
                    return
            except Exception as e:
                logger.error(f"[羊村基金涨跌榜] 何时更新图片生成失败: {e}")
            await self._send_prompt(event, group_id, await self._cmd_update_analysis_text(event))
            event.stop_event()
            return
        if "手动更新" in text or "手动刷新" in text or "更新基金" in text:
            # 手动触发一次基金更新：只更新已确认(confirmed=1)持仓，完全不影响待确认数据
            try:
                total, waiting = await self._do_update()
                pend = self._storage.pending_records()
                parts = []
                if total:
                    parts.append(f"✅ 已更新 {total} 条")
                if waiting:
                    parts.append(f"⏳ {waiting} 只净值还没出，晚点自动补")
                if pend:
                    parts.append(f"待确认 {len(pend)} 条不动")
                msg = "\n".join(parts) if parts else "这次没得更新，净值还没发布。"
                msg += "\n回复「涨跌榜」看最新"
                await self._send_prompt(event, group_id, msg, recall_delay=0)
            except Exception as e:
                logger.error(f"[羊村基金涨跌榜] 手动更新失败: {e}")
                await self._send_prompt(event, group_id, f"手动更新失败：{e}")
            event.stop_event()
            return

    async def _cmd_update_analysis(self, event) -> str | None:
        """联网分析基金更新状态并按成员分组渲染成图片；失败返回 None。"""
        recs, scope = self._collect_update_records(event)
        if not recs:
            return None
        groups = []
        by_member: dict[str, list] = {}
        for r in recs:
            by_member.setdefault(r["member"], []).append(r)
        for member in sorted(by_member):
            funds = []
            for r in by_member[member]:
                nav_date, change, _ = _fetch_nav_info(r["fund_code"])
                name = r["fund_name"] or r["fund_code"]
                funds.append({"fund_name": name, "fund_code": r["fund_code"],
                              "nav_date": nav_date, "change": change,
                              "confirmed": r["confirmed"],
                              "rule": _fund_confirm_rule(name)})
            groups.append({"member": member, "funds": funds})
        return self._render_update_image(groups)

    def _collect_update_records(self, event) -> tuple[list, str]:
        """收集待分析的基金记录：@他人 → 该成员持仓；否则全部。返回 (记录列表, 范围名)。"""
        at_name = self._at_other_name(event)
        all_recs = self._live(self._storage.all_records())
        if at_name:
            recs = [r for r in all_recs if self._member_matches(r["member"], at_name)]
            return recs, at_name
        return all_recs, "全部成员"

    async def _cmd_update_analysis_text(self, event) -> str:
        """联网分析基金更新状态，输出文本（图片失败的兜底）。"""
        recs, scope = self._collect_update_records(event)
        if not recs:
            return "暂无基金数据，请先「导入数据」+截图。"
        today = datetime.now().strftime("%Y-%m-%d")
        seen = set()
        uniq = []
        for r in recs:
            if r["fund_code"] not in seen:
                seen.add(r["fund_code"])
                uniq.append(r)
        lines = [f"🕐 基金更新状态分析（{scope}）", "━━━━━━━━━━━━"]
        for r in uniq:
            code = r["fund_code"]
            name = r["fund_name"] or code
            nav_date, change, _ = _fetch_nav_info(code)
            if nav_date:
                if nav_date == today:
                    st = "今日已更新"
                else:
                    st = f"最新 {nav_date}（今日未出）"
                ch = f"{change:+.2f}%" if change is not None else ""
            else:
                st, ch = "联网失败", ""
            pending = "待确认" if r["confirmed"] == 0 else "已确认"
            rule = _fund_confirm_rule(name)
            lines.append(f"· {name}({code})")
            lines.append(f"  净值：{st} {ch}｜状态：{pending}｜{rule}")
        lines.append("━━━━━━━━━━━━")
        lines.append("净值一般 20:00 后发布当日净值；确认份额按 T+N 交易日到账。")
        return "\n".join(lines)

    # ---------- 删除基金 / 修改数据 ----------

    def _start_edit(self, event, umo: str, sender: str, mode: str) -> str:
        """「删除基金/修改数据」入口：列出该成员记录，让用户选编号。"""
        member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
        recs = [r for r in self._storage.all_records()
                if self._member_matches(r["member"], member)]
        if not recs:
            return f"📋 {member} 名下暂无记录，请先「导入数据」+截图。"
        self._edit_select[(umo, sender)] = mode
        self._state_ts.setdefault((umo, sender), {})["edit"] = time.time()
        self._edit_records[(umo, sender)] = recs
        verb = "删除" if mode == "delete" else "修改"
        lines = [f"📋 {member} 的记录（回复编号即可{verb}）："]
        for i, r in enumerate(recs, 1):
            fund = r["fund_name"] or r["fund_code"]
            tag = "（待确认）" if not r["confirmed"] else ""
            lines.append(f"  {i}. {fund}({r['fund_code']}) ¥{r['current']:,.2f} {tag}")
        if mode == "delete":
            lines.append("回复数字（空格分隔）删除对应条目，例如「1 2」。")
        else:
            lines.append("回复「编号 新金额」改本金，例如「1 15000」；"
                          "或「编号 确认/待确认」切换确认状态（调试用）。")
        return "\n".join(lines)

    async def _handle_edit_select(self, event, umo: str, sender: str) -> str:
        mode = self._edit_select.get((umo, sender))
        recs = self._edit_records.get((umo, sender))
        text = (event.message_str or "").strip()
        if not mode or not recs:
            return "操作已失效，请重新发「删除基金」或「修改数据」。"
        nums = re.findall(r"\d+(?:\.\d+)?", text)
        if mode == "delete":
            sel = [recs[i - 1] for i in [int(n) for n in nums]
                   if 1 <= i <= len(recs)]
            if not sel:
                return f"编号无效，可选范围 1-{len(recs)}。"
            n = 0
            for r in sel:
                n += self._storage.delete_record(r["member"], r["fund_code"])
            self._edit_select.pop((umo, sender), None)
            self._edit_records.pop((umo, sender), None)
            names = "、".join(r["fund_name"] or r["fund_code"] for r in sel)
            return f"🗑️ 已删除 {n} 条记录：{names}"
        else:  # modify
            if len(nums) < 1:
                return "请回复「编号 新金额」或「编号 确认/待确认」，例如「1 15000」或「2 待确认」。"
            idx = int(nums[0])
            if not (1 <= idx <= len(recs)):
                return f"编号无效，可选范围 1-{len(recs)}。"
            r = recs[idx - 1]
            rname = r["fund_name"] or r["fund_code"]
            # 调试用：切换确认状态
            if re.search(r"确认|转正|已确认", text):
                n = self._storage.confirm_member_codes(r["member"], [r["fund_code"]])
                self._edit_select.pop((umo, sender), None)
                self._edit_records.pop((umo, sender), None)
                if n:
                    return (f"✅ 已把 {rname} 转正为【已确认】"
                            f"{'（含买入挂账并入本金）' if r.get('pending_amount') else ''}。")
                return f"{rname} 已是已确认状态或无需转正。"
            if re.search(r"待确认|取消确认|未确认|不确认", text):
                self._storage.set_pending(r["member"], r["fund_code"])
                self._edit_select.pop((umo, sender), None)
                self._edit_records.pop((umo, sender), None)
                return (f"⏳ 已把 {rname} 改为【待确认】，资金回到本金，"
                        f"不参与每日涨跌（买入挂账保留）。")
            # 改本金
            if len(nums) < 2:
                return "请回复「编号 新金额」或「编号 确认/待确认」。"
            amount = float(nums[-1])
            if amount <= 0:
                return "金额需大于 0。"
            self._storage.update_principal(r["member"], r["fund_code"], amount)
            self._edit_select.pop((umo, sender), None)
            self._edit_records.pop((umo, sender), None)
            return (f"✏️ 已把 {rname} 本金改为 ¥{amount:,.2f}，现资金已同步重置。")

    # ---------- 买入基金 ----------

    async def _start_buy(self, event, umo: str, sender: str) -> str:
        """「买入基金」入口：列出该成员记录供选择追加，或直接输入新基金名。"""
        member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
        recs = [r for r in self._storage.all_records()
                if self._member_matches(r["member"], member)]
        self._buy_select[(umo, sender)] = {"member": member, "records": recs, "prompt_mid": None}
        self._state_ts.setdefault((umo, sender), {})["buy"] = time.time()
        lines = ["💹 买入基金 —— 选择已有持仓追加，或直接输入新基金名。"]
        if recs:
            lines.append("你的现有持仓（回复「编号 金额」追加，例如「1 3000」）：")
            for i, r in enumerate(recs, 1):
                pa = r.get("pending_amount") or 0.0
                if not r["confirmed"]:
                    tag = "（整条待确认）"
                elif pa > 0:
                    tag = f"（+买入待确认 ¥{pa:,.0f}）"
                else:
                    tag = ""
                lines.append(f"  {i}. {r['fund_name'] or r['fund_code']}({r['fund_code']}) "
                             f"¥{r['current']:,.2f} {tag}")
        else:
            lines.append("你名下暂无记录，可直接输入「基金名 金额」，例如「招商白酒 5000」。")
        lines.append("也可附确认时间，如「1 3000 周四确认」。")
        lines.append("买入已确认份额继续在榜算涨跌；新买入份额先挂账【待确认】，确认后才并入本金（未给时间可稍后「预约确认」）。")
        return "\n".join(lines)

    async def _handle_buy_select(self, event, umo: str, sender: str) -> str:
        st = self._buy_select.get((umo, sender))
        if not st:
            return "买入状态已失效，请重新发「买入基金」。"
        member = st["member"]
        recs = st["records"]
        text = (event.message_str or "").strip()
        parsed = await _llm_parse_buy_input(text, len(recs))
        amount = parsed.get("amount", 0.0)
        if amount <= 0:
            return "没识别到买入金额，请回复如「1 3000」或「招商白酒 5000」。"
        # 确定目标基金：优先编号命中已有持仓，其次名称匹配，否则按名称新建
        code = name = None
        is_new = False
        idx = parsed.get("index")
        if idx and 1 <= idx <= len(recs):
            r = recs[idx - 1]
            code, name = r["fund_code"], r["fund_name"] or r["fund_code"]
        else:
            nm = parsed.get("name", "").strip()
            matched = None
            if nm:
                for r in recs:
                    if (nm and (nm in r["fund_name"] or nm in r["fund_code"]
                                or r["fund_name"] in nm or r["fund_code"] in nm)):
                        matched = r
                        break
            if matched:
                code, name = matched["fund_code"], matched["fund_name"] or matched["fund_code"]
            else:
                if not nm:
                    return "请回复要买入的基金：编号（如「1 3000」）或基金名（如「招商白酒 5000」）。"
                got = _resolve_fund(nm)
                if not got:
                    return f"未能识别/找到基金『{nm}』，请提供基金代码或更准确的名称。"
                code, name, is_new = got[0], got[1], True
        if not code:
            return "未能确定要买入的基金，请重试。"
        bf = self._storage.get_fund(member, code)
        before_confirmed = bf["confirmed"] if bf else None
        affected, is_new = self._storage.buy_increase(member, code, name, amount)
        # 可选：预约确认时间（附带则直接预约到点转正）
        ct = parsed.get("confirm_time")
        if ct:
            try:
                dt = datetime.strptime(ct, "%Y-%m-%d %H:%M")
                self._confirm_schedule[(member, code)] = dt
                self._storage.save_schedule(member, code, ct)
            except ValueError:
                ct = ""  # 时间格式异常则走自动询问确认时间
        self._buy_select.pop((umo, sender), None)
        self._state_ts.pop((umo, sender), None)
        rec_now = None
        for r in self._storage.all_records():
            if r["member"] == member and r["fund_code"] == code:
                rec_now = r
                break
        if is_new:
            status = "已新建买入并标记【整条待确认】"
        elif rec_now and rec_now["confirmed"] == 1:
            pa = rec_now.get("pending_amount") or 0.0
            status = (f"已确认份额 ¥{rec_now['current']:,.2f} 继续在榜算涨跌，"
                      f"买入 ¥{pa:,.2f} 挂账【待确认】")
        else:
            status = "已并入待确认本金（该条仍为待确认）"
        if ct:
            return (f"✅ 已买入 {name}({code}) ¥{amount:,.2f}。{status}。\n"
                    f"份额确认预约于 {ct} 自动转正。\n回复「涨跌榜」查看数据。")
        # 未附确认时间 → 自动进入确认时间询问，保留取消（撤销本次买入）
        self._buy_confirm[(umo, sender)] = {
            "member": member, "code": code, "name": name, "amount": amount,
            "before_confirmed": before_confirmed, "was_new": is_new, "prompt_mid": None,
        }
        self._state_ts.setdefault((umo, sender), {})["buy_confirm"] = time.time()
        return (f"✅ 已买入 {name}({code}) ¥{amount:,.2f}。{status}。\n"
                f"请回复份额确认时间（如「周四15:00」「明天确认」），到点自动转正；\n"
                f"回复「取消」可撤销本次买入。")

    def _undo_buy(self, st: dict) -> str:
        """撤销一次已入库的买入（确认时间阶段回复「取消」时调用），并清理该笔预约。"""
        if not st:
            return "已取消买入，未作改动。"
        member, code = st["member"], st["code"]
        self._storage.undo_buy(member, code, st["amount"],
                               st.get("before_confirmed"), st.get("was_new", False))
        key = (member, code)
        if key in self._confirm_schedule:
            self._confirm_schedule.pop(key, None)
            self._storage.remove_schedules(member, code)
        return (f"已取消买入 {st['name']}({code}) ¥{st['amount']:,.2f}，已撤销入库，数据无改动。")

    # ---------- 卖出基金 ----------

    async def _start_sell(self, event, umo: str, sender: str) -> str:
        """「卖出基金」入口：列出该成员已确认持仓供选择卖出。"""
        member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
        recs = [r for r in self._storage.all_records()
                if self._member_matches(r["member"], member)]
        if not recs:
            return f"📋 {member} 名下暂无持仓，请先「导入数据」+截图。"
        self._sell_select[(umo, sender)] = {"member": member, "records": recs, "prompt_mid": None}
        self._state_ts.setdefault((umo, sender), {})["sell"] = time.time()
        lines = [f"📉 卖出基金 —— 选择要卖出的持仓（只能卖已确认份额，待确认份额不受影响）。"]
        for i, r in enumerate(recs, 1):
            fund = r["fund_name"] or r["fund_code"]
            if not r["confirmed"]:
                lines.append(f"  {i}. {fund}({r['fund_code']}) 整条待确认，暂不可卖")
            else:
                lines.append(f"  {i}. {fund}({r['fund_code']}) 可卖 ¥{r['principal']:,.2f}")
        lines.append("回复「编号 金额」卖出，如「1 2000」；「编号 全部」清空该条已确认本金；")
        lines.append("回复「取消」不卖了。卖出即扣减已确认本金并留痕，可发「交易明细」查看。")
        return "\n".join(lines)

    async def _handle_sell_select(self, event, umo: str, sender: str) -> str:
        st = self._sell_select.get((umo, sender))
        if not st:
            return "卖出状态已失效，请重新发「卖出基金」。"
        member = st["member"]
        recs = st["records"]
        text = (event.message_str or "").strip()
        nums = [int(n) for n in re.findall(r"\d+", text)]
        if not nums:
            return "没识别到编号，请回复「编号 金额」卖出，如「1 2000」，或「取消」退出。"
        idx = nums[0]
        if not (1 <= idx <= len(recs)):
            return f"编号 {idx} 不在列表里（共 {len(recs)} 条），请重新回复。"
        r = recs[idx - 1]
        if not r["confirmed"]:
            return (f"{r['fund_name'] or r['fund_code']}({r['fund_code']}) 还是整条待确认，"
                    f"份额确认后才能卖出（可「预约确认」或「确认份额」先转正）。")
        if r["principal"] <= 0:
            return f"{r['fund_name'] or r['fund_code']}({r['fund_code']}) 已确认本金为 0，没有可卖的份额。"
        # 解析卖出金额：支持「全部 / 全部卖出 / 清仓」，以及中文单位（1.5万=15000、2千=2000）
        if re.search(r"全部|清仓|清空|all", text, re.I):
            amount = r["current"]
        else:
            amount = 0.0
            mw = re.search(r"([\d]+(?:\.\d+)?)\s*万", text)
            if mw:
                amount = float(mw.group(1)) * 10000
            else:
                mq = re.search(r"([\d]+(?:\.\d+)?)\s*千", text)
                if mq:
                    amount = float(mq.group(1)) * 1000
            if amount <= 0:
                for tok in re.findall(r"[\d,]+\.?\d*", text):
                    try:
                        v = float(tok.replace(",", ""))
                    except ValueError:
                        continue
                    if v >= 1 and v != idx:
                        amount = v
                        break
            if amount <= 0:
                return (f"没识别到卖出金额，请回复「编号 金额」（金额=卖出到手），如「{idx} {r['current']:,.0f}」，"
                        f"或「{idx} 全部」清空该条。")
        rows, new_p, pnl, realized, err = self._storage.sell_decrease(member, r["fund_code"], amount)
        if rows <= 0:
            return f"卖出失败：{err or '记录不存在。'}"
        self._storage.add_trade_log(member, r["fund_code"], r["fund_name"] or r["fund_code"],
                                    "卖出", amount)
        self._sell_select.pop((umo, sender), None)
        self._state_ts.pop((umo, sender), None)
        name = r["fund_name"] or r["fund_code"]
        tail = ""
        if r.get("pending_amount"):
            tail = f"\n注：该条还有买入待确认 ¥{r['pending_amount']:,.2f}，确认后才会并入本金，不受本次卖出影响。"
        if new_p <= 0 and not r.get("pending_amount"):
            tail += f"\n该条已确认本金已清空，可回复「删除基金」彻底移除这条记录。"
        return (f"✅ 已卖出 {name}({r['fund_code']}) ¥{amount:,.2f}，"
                f"剩余已确认本金 ¥{new_p:,.2f}。\n"
                f"📊 本次卖出盈亏 {pnl:+,.2f}，该基金累计已实现 {realized:+,.2f}。\n"
                f"已记入「交易明细」。{tail}")

    # ---------- 基金转换（在途模式：转出日之前源基金继续跟净值，到转出日晚自动结算）----------

    async def _start_convert(self, event, umo: str, sender: str) -> str:
        """「转换基金」入口（图片渲染失败时的文字兜底）：列出可转出基金。"""
        member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
        busy = set((self._convert_select.get((umo, sender)) or {}).get("busy") or ())
        recs = [r for r in self._storage.all_records()
                if (self._member_matches(r["member"], member))
                and r["confirmed"] and r["principal"] > 0 and r["fund_code"] not in busy]
        lines = ["🔄 基金转换（在途模式）—— 把 A 基金整只或部分转投到 B。"]
        lines.append("转出日之前 A 基金继续跟净值（涨跌还算你的），到转出日晚自动按当天净值结算、钱挂进 B。")
        lines.append("选择要转出的基金（回复编号，可带金额/比例）：")
        for i, r in enumerate(recs, 1):
            fund = r["fund_name"] or r["fund_code"]
            lines.append(f"  {i}. {fund}({r['fund_code']}) 市值 ¥{r['current']:,.2f}"
                         + (f"｜{float(r['shares']):,.2f} 份" if float(r.get("shares") or 0) > 0 else ""))
        lines.append("例：「1」整只、「1 100.5」转出 100.5 份、「1 50%」转一半、"
                     "「1 金额100」按 100 元转；回复「取消」退出。")
        lines.append("💡 转换按份额算：转出份额×转出日净值=转出金额，转入金额÷确认日净值=转入份额。")
        return "\n".join(lines)

    async def _handle_convert_source(self, event, umo: str, sender: str) -> str:
        st = self._convert_select.get((umo, sender))
        if not st:
            return "转换状态已失效，请重新发「转换基金」。"
        recs = st["records"]
        text = (event.message_str or "").strip()
        # 剥掉 @ / 引用 / 表情标记再取编号（否则 <@4A8C…> 里的数字会被当成编号）
        clean = re.sub(r"\[[^\]]*\]|<@!?[^>]*>|<[^>]{1,60}>", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        m = re.fullmatch(r"(\d{1,2})\s*(?:全部|清仓|清空|all)?\s*(.*)", clean, re.I)
        if not m:
            return ("请回复要转出的编号，可带份额或百分比：\n"
                    "　「1」= 整只转出　「1 100.5」= 转出 100.5 份　「1 50%」= 转一半\n"
                    "　（想按金额就说「1 金额100」）")
        idx = int(m.group(1))
        if not (1 <= idx <= len(recs)):
            return f"编号 {idx} 不在列表里（共 {len(recs)} 条），请重选。"
        r = recs[idx - 1]
        if r["fund_code"] in set(st.get("busy") or ()):
            return (f"【{r['fund_name'] or r['fund_code']}】已经有一笔在途转换了，"
                    f"同一只基金不能同时转两次（会把同一笔钱清零两次）。换一只，或先「取消转换」那笔。")
        cur_val = float(r["current"] or 0.0)
        sh_total = float(r.get("shares") or 0.0)
        part_amount = 0.0
        part_ratio = 0.0
        out_shares = 0.0
        raw = (m.group(2) or "").strip()
        if raw:
            tail = re.sub(r"[\s,，]", "", raw)
            money = re.match(r"^(?:金额|钱|本金|元)([\d.]+)$", tail) or re.match(r"^([\d.]+)元$", tail)
            try:
                if tail.endswith("%"):
                    pct = float(tail[:-1])
                    if not (0 < pct <= 100):
                        return "比例要在 0~100% 之间，例「1 50%」；整只转出就回「1」。"
                    part_ratio = round(pct / 100.0, 6)
                elif money:
                    part_amount = round(float(money.group(1)), 2)
                else:
                    out_shares = round(float(tail.rstrip("份")), 4)
            except ValueError:
                return ("份额/比例没看懂，例：「1 100.5」（转出 100.5 份）、「1 50%」（转一半）、"
                        "「1 金额100」（按金额 100 元转）。")
            if money or out_shares > 0:
                nav_latest = _fetch_latest_nav(r["fund_code"])
                nav = nav_latest[1] if nav_latest else 0.0
                if part_amount > 0 and nav > 0:
                    out_shares = round(part_amount / nav, 4)
                if out_shares > 0 and nav > 0:
                    part_amount = round(out_shares * nav, 2)
                elif out_shares > 0 and sh_total > 0:
                    part_amount = round(cur_val * out_shares / sh_total, 2)
            if part_amount <= 0 and out_shares <= 0 and part_ratio <= 0:
                return "转出份额要大于 0；整只转出就回「1」。"
            if out_shares > 0 and sh_total > 0 and out_shares >= sh_total - 0.0001:
                part_amount, part_ratio, out_shares = 0.0, 0.0, 0.0  # 全部份额 = 整只
            elif part_ratio > 0 and part_ratio >= 1:
                part_amount, part_ratio, out_shares = 0.0, 0.0, 0.0
            elif part_amount >= cur_val - 0.005:
                part_amount, part_ratio, out_shares = 0.0, 0.0, 0.0
        st["stage"] = "target"
        st["src_code"] = r["fund_code"]
        st["src_name"] = r["fund_name"] or r["fund_code"]
        st["part_amount"] = part_amount
        st["part_ratio"] = part_ratio
        st["out_shares"] = out_shares
        # 转入候选：该成员其它有现值的已确认持仓，可以直接回编号
        cands = [x for x in self._storage.all_records()
                 if (x["member"] == st["member"] or st["member"] in x["member"])
                 and x["confirmed"] and x["fund_code"] != r["fund_code"]
                 and float(x["current"] or 0) > 0]
        st["cands"] = cands
        if out_shares > 0:
            desc = (f"转出 {out_shares:,.2f} 份（按最新净值约 ¥{part_amount:,.2f}，"
                    f"源基金保留 {sh_total - out_shares:,.2f} 份）" if sh_total > 0
                    else f"转出 {out_shares:,.2f} 份（按最新净值约 ¥{part_amount:,.2f}）")
        elif part_amount > 0:
            desc = f"部分转出 ¥{part_amount:,.2f}（源基金保留约 ¥{cur_val - part_amount:,.2f}）"
        else:
            desc = f"整只转出 ¥{cur_val:,.2f}"
        lines = [f"已选转出【{st['src_name']}({r['fund_code']})】：{desc}。",
                 "　（转出日之前它继续跟净值，现在不动）",
                 "转入哪只？（回编号，或直接回基金名 / 6 位代码）"]
        for i, x in enumerate(cands, 1):
            lines.append(f"  {i}. {x['fund_name'] or x['fund_code']}({x['fund_code']}) "
                         f"¥{float(x['current']):,.2f}")
        lines.append("要转进一只没有持仓的基金，直接回名称或 6 位代码，如「002053」。")
        return "\n".join(lines)

    async def _handle_convert_target(self, event, umo: str, sender: str) -> str:
        st = self._convert_select.get((umo, sender))
        if not st:
            return "转换状态已失效，请重新发「转换基金」。"
        text = (event.message_str or "").strip()
        member = st["member"]
        cands = st.get("cands") or []
        clean = re.sub(r"\[[^\]]*\]|<@!?[^>]*>|<[^>]{1,60}>", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        mnum = re.fullmatch(r"(\d{1,2})", clean)
        if mnum and 1 <= int(mnum.group(1)) <= len(cands):
            pick = cands[int(mnum.group(1)) - 1]
            st["tgt_code"] = pick["fund_code"]
            st["tgt_name"] = pick["fund_name"] or pick["fund_code"]
            return self._prepare_convert_review(st)
        if text.strip() == st["src_code"] or (st["src_name"] and text.strip() == st["src_name"]):
            return "转入基金不能和转出基金是同一只，请换一个。"
        code = name = None
        for r in self._storage.all_records():
            if r["member"] != member and member not in r["member"]:
                continue
            fn = r["fund_name"] or ""
            if text and (text in fn or text in r["fund_code"]
                         or (fn and fn in text) or r["fund_code"] in text):
                code, name = r["fund_code"], fn or r["fund_code"]
                break
        if not code:
            got = _resolve_fund(text)
            if not got:
                return f"没能找到基金『{text}』，回编号、基金全名或 6 位代码，或「取消」。"
            code, name = got
        if code == st["src_code"]:
            return "转入基金不能和转出基金是同一只，请换一个。"
        st["tgt_code"] = code
        st["tgt_name"] = name
        return self._prepare_convert_review(st)

    def _convert_anchor(self) -> str:
        return (self.cfg.get("default_confirm_time") or _DEFAULT_CONFIRM_TIME)

    def _prepare_convert_review(self, st: dict) -> str:
        """选好转入后，按交易日规则填好默认转出日/确认时间，进入确认单。"""
        name = st.get("tgt_name") or ""
        st["qdii"] = ("QDII" in name.upper()) or ("美元" in name)
        st["stage"] = "review"
        st["out_date"] = _default_convert_out_date()
        st["confirm_at"] = _default_convert_confirm_at(
            st["out_date"], st["qdii"], self._convert_anchor())
        st["review_note"] = ""
        return self._render_convert_review(st)

    def _render_convert_review(self, st: dict) -> str:
        rec = self._storage.get_fund(st["member"], st["src_code"]) or {}
        cur = float(rec.get("current") or 0.0)
        prin = float(rec.get("principal") or 0.0)
        pnl = cur - prin
        wk = "一二三四五六日"
        od = datetime.strptime(st["out_date"], "%Y-%m-%d")
        note = (st.get("review_note") or "").strip()
        st["review_note"] = ""
        part = float(st.get("part_amount") or 0.0)
        out_sh = float(st.get("out_shares") or 0.0)
        sh_total = float(rec.get("shares") or 0.0)
        if out_sh > 0:
            scope = (f"转出 {out_sh:,.2f} 份（按最新净值约 ¥{part:,.2f}），"
                     f"保留 {max(sh_total - out_sh, 0.0):,.2f} 份")
        elif 0 < part < cur - 0.005:
            scope = f"部分转出 ¥{part:,.2f}，剩余约 ¥{cur - part:,.2f}"
        else:
            scope = f"整只转出 ¥{cur:,.2f}"
        lines = [(note + "🔄 转换确认单") if note else "🔄 转换确认单",
                 f"　转出 【{st['src_name']}】({st['src_code']})　现值 ¥{cur:,.2f}",
                 f"　　　成本 ¥{prin:,.2f}（{'浮盈' if pnl >= 0 else '浮亏'} ¥{pnl:+,.2f}）· {scope}",
                 f"　转入 【{st['tgt_name']}】({st['tgt_code']})",
                 f"　转出日 {st['out_date']}（周{wk[od.weekday()]}）· 当晚按当天净值结算",
                 f"　份额确认 {st['confirm_at']}（{'QDII T+2' if st.get('qdii') else 'T+1'}）· 到点我提示你确认",
                 "",
                 "回「确认」按上面的日期下单；",
                 "　改份额：「转出份额 100.5」　改金额：「转出金额 100」或「转出金额 全部」",
                 "　改日期：「转出日 周五」　「确认时间 周五15:00」",
                 "　「取消」放弃，不动任何数据。",
                 "💡 结算额是毛额，实际到账要扣转换费（赎回费+申购补差）。"]
        return "\n".join(lines)

    async def _handle_convert_review(self, event, umo: str, sender: str) -> str:
        st = self._convert_select.get((umo, sender))
        if not st:
            return "转换状态已失效，请重新发「转换基金」。"
        text = (event.message_str or "").strip()
        low = re.sub(r"[\s!！。.,，~～]", "", text.lower())
        if low in ("确认", "确定", "下单", "提交", "可以", "好", "ok", "okay", "y", "yes", "对", "没问题"):
            return self._commit_convert(st, umo, sender)
        m_sh = re.search(r"(?:转出份额|份额)\s*[:：]?\s*([\d.]+)", text)
        if m_sh:
            rec = self._storage.get_fund(st["member"], st["src_code"]) or {}
            cur = float(rec.get("current") or 0.0)
            sh_total = float(rec.get("shares") or 0.0)
            try:
                v = round(float(m_sh.group(1)), 4)
            except ValueError:
                return "份额没看懂，例：「转出份额 100.5」。"
            if v <= 0:
                return "转出份额要大于 0；整只转出就回「转出金额 全部」。"
            if sh_total > 0 and v >= sh_total - 0.0001:
                st["out_shares"] = 0.0
                st["part_amount"] = 0.0
                st["part_ratio"] = 0.0
            else:
                nav_latest = _fetch_latest_nav(st["src_code"])
                nav = nav_latest[1] if nav_latest else 0.0
                st["out_shares"] = v
                st["part_ratio"] = 0.0
                st["part_amount"] = (round(v * nav, 2) if nav > 0 and sh_total <= 0
                                     else round(cur * v / sh_total, 2) if sh_total > 0 else 0.0)
            return self._render_convert_review(st)
        m_amt = re.search(r"(?:转出金额|转多少钱|金额)\s*[:：]?\s*([\d.]+|全部|全量)", text)
        if m_amt:
            rec = self._storage.get_fund(st["member"], st["src_code"]) or {}
            cur = float(rec.get("current") or 0.0)
            raw = m_amt.group(1)
            st["out_shares"] = 0.0
            if raw in ("全部", "全量"):
                st["part_amount"] = 0.0
            else:
                try:
                    v = round(float(raw), 2)
                except ValueError:
                    return "金额没看懂，例：「转出金额 100」或「转出金额 全部」。"
                if v <= 0:
                    return "转出金额要大于 0；整只转出就回「转出金额 全部」。"
                st["part_amount"] = 0.0 if v >= cur - 0.005 else v
                st["part_ratio"] = 0.0
            return self._render_convert_review(st)
        want_confirm = (bool(re.search(r"(确认时间|确认日|到账|份额|转正)", text))
                        and not re.search(r"转出日", text))
        dt = await _llm_parse_confirm_time(text)
        if not dt:
            return "没看懂。回「确认」下单；改日期回「转出日 周五」或「确认时间 周五15:00」。"
        if want_confirm:
            ca = dt.strftime("%Y-%m-%d %H:%M")
            if dt.strftime("%Y-%m-%d") <= st["out_date"]:
                ca = _default_convert_confirm_at(st["out_date"], st.get("qdii"),
                                                 dt.strftime("%H:%M"))
                st["review_note"] = f"⚠️ 份额确认必须晚于转出日，已顺延到 {ca}。\n"
            st["confirm_at"] = ca
        else:
            out = dt.strftime("%Y-%m-%d")
            if not _is_market_open(out):
                fixed = _next_trading_day(out)
                st["review_note"] = f"⚠️ {out} 休市（周末/节假日），转出日顺延到 {fixed}。\n"
                out = fixed
            if out < time.strftime("%Y-%m-%d"):
                out = _default_convert_out_date()
                st["review_note"] = f"⚠️ 转出日不能早于今天，已改为 {out}。\n"
            st["out_date"] = out
            st["confirm_at"] = _default_convert_confirm_at(
                out, st.get("qdii"), self._convert_anchor())
        return self._render_convert_review(st)

    def _commit_convert(self, st: dict, umo: str, sender: str) -> str:
        part = round(float(st.get("part_amount") or 0.0), 2)
        cid = self._storage.create_conversion(
            st["member"], st["src_code"], st["src_name"], st["tgt_code"], st["tgt_name"],
            st["out_date"], st["confirm_at"], part_amount=part,
            part_ratio=float(st.get("part_ratio") or 0.0),
            out_shares=float(st.get("out_shares") or 0.0))
        self._convert_select.pop((umo, sender), None)
        self._state_ts.pop((umo, sender), None)
        tgt = self._storage.get_fund(st["member"], st["tgt_code"])
        if tgt and tgt["confirmed"]:
            handover = "转入基金你本来就持有 → 结算后单独挂一笔账，到点按这一笔的金额确认，不和别的批次混。"
        else:
            handover = "转入基金是新记录 → 结算后先记「待确认」，到点提示你确认后并入本金。"
        out_sh = float(st.get("out_shares") or 0.0)
        if out_sh > 0:
            scope = f"转出 {out_sh:,.2f} 份（≈¥{part:,.2f}，剩下的继续跟净值）"
        elif part > 0:
            scope = f"部分转出 ¥{part:,.2f}（源基金剩余继续跟净值）"
        else:
            scope = "整只转出"
        return (f"✅ 已登记在途转换 #{cid}（{scope}）\n"
                f"　【{st['src_name']}】→【{st['tgt_name']}({st['tgt_code']})】\n"
                f"　转出日 {st['out_date']}，当晚按当天净值结算并挂账；\n"
                f"　{handover}\n"
                f"　转出日之前源基金继续在榜算涨跌；回「转换状态」看进度，「取消转换」可撤销。")

    def _render_convert_status(self, member: str) -> str:
        """「转换状态」：在途 + 最近已结算，含每笔的批次确认状态。"""
        act = self._storage.active_conversions(member)
        recent = self._storage.recent_conversions(member, 8)
        lots: dict = {}
        for l in (self._storage.pending_lots(member=member, status="pending")
                  + self._storage.pending_lots(member=member, status="confirmed")):
            lots.setdefault(l["conversion_id"], l)
        today = time.strftime("%Y-%m-%d")
        lines = [f"🔄 {member} 的基金转换"]
        if act:
            lines.append(f"在途 {len(act)} 笔：")
            for i, c in enumerate(act, 1):
                rec = self._storage.get_fund(member, c["src_code"]) or {}
                est = float(rec.get("current") or 0.0)
                try:
                    dd = (datetime.strptime(c["out_date"], "%Y-%m-%d")
                          - datetime.strptime(today, "%Y-%m-%d")).days
                except Exception:
                    dd = None
                when = ("今天" if dd == 0 else f"{dd} 天后" if dd and dd > 0
                        else f"{-dd} 天前（等净值）" if dd else "")
                lines.append(f"  {i}. 【{c['src_name'] or c['src_code']}】→"
                             f"【{c['tgt_name'] or c['tgt_code']}】")
                part = float(c.get("part_amount") or 0.0)
                ratio = float(c.get("part_ratio") or 0.0)
                out_sh = float(c.get("out_shares") or 0.0)
                if out_sh > 0:
                    scope = f"转出 {out_sh:,.2f} 份（≈¥{part:,.2f}）" if part > 0 \
                        else f"转出 {out_sh:,.2f} 份"
                elif ratio > 0:
                    scope = f"部分 {ratio * 100:.0f}%（≈¥{est * ratio:,.2f}）"
                elif part > 0:
                    scope = f"部分 ¥{part:,.2f}"
                else:
                    scope = "整只"
                lines.append(f"     净值日 {c['out_date']}{('（' + when + '）') if when else ''}"
                             f"｜确认 {c['confirm_at'] or '—'}"
                             f"｜{scope}（现值 ¥{est:,.2f}）")
        else:
            lines.append("在途 0 笔。")
        done = [c for c in recent if c["status"] != "active"]
        if done:
            lines.append("最近已结算：")
            for c in done[:4]:
                lot = lots.get(c["id"])
                if lot:
                    tag = ("份额已确认 ✅" if lot["status"] == "confirmed"
                           else f"等 {lot['confirm_at']} 确认份额")
                else:
                    tag = "份额已确认 ✅"
                part_tag = ("部分 " if (float(c.get("part_amount") or 0) > 0
                                       or float(c.get("part_ratio") or 0) > 0
                                       or float(c.get("out_shares") or 0) > 0) else "")
                sh_note = ""
                if float(c.get("out_shares") or 0) > 0:
                    sh_note = f"转出 {float(c['out_shares']):,.2f} 份"
                if float(c.get("in_shares") or 0) > 0:
                    sh_note += (f" → 转入 {float(c['in_shares']):,.2f} 份"
                                f"（净值 {float(c.get('in_nav') or 0):.4f}）")
                lines.append(f"  #{c['id']} {c['src_code']}→{c['tgt_code']} "
                             f"{part_tag}¥{float(c['amount'] or 0):,.2f}｜{tag}"
                             + (f"｜{sh_note}" if sh_note else ""))
        lines.append("「转换基金」新建，「取消转换」撤销在途（可带序号）。")
        return "\n".join(lines)

    def _set_convert_shares(self, member: str, raw: str) -> str:
        """把在途的那笔转换改成「按份额」：转出份额 X。用于截图里是份额、当初按金额登记的情况。"""
        active = self._storage.active_conversions(member)
        if not active:
            return "当前没有在途转换，先用「转换基金」登记一笔。"
        if len(active) > 1:
            return (f"你有 {len(active)} 笔在途转换，先在「转换状态」里看清是哪笔，"
                    f"再「取消转换 序号」重登，避免改错。")
        c = active[0]
        try:
            v = round(float(str(raw).strip().rstrip("份")), 4)
        except ValueError:
            return "份额没看懂，例：「转换份额 100.8」。"
        if v <= 0:
            return "份额要大于 0。"
        src = self._storage.get_fund(member, c["src_code"]) or {}
        sh_total = float(src.get("shares") or 0.0)
        cur = float(src.get("current") or 0.0)
        if sh_total > 0 and v >= sh_total - 0.0001:
            return (f"【{c['src_name'] or c['src_code']}】一共才 {sh_total:,.2f} 份，"
                    f"整只转出就「取消转换」后重登，或份额填小一点。")
        nav = _fetch_latest_nav(c["src_code"])
        nav_v = nav[1] if nav else 0.0
        est = round(v * nav_v, 2) if nav_v > 0 else (
            round(cur * v / sh_total, 2) if sh_total > 0 else 0.0)
        con = _db_connect(_db_path())
        try:
            con.execute("UPDATE conversions SET out_shares=?, part_ratio=?, part_amount=? "
                        "WHERE id=?",
                        (v, round(v / sh_total, 6) if sh_total > 0 else 0.0, est, c["id"]))
            con.commit()
        finally:
            con.close()
        return (f"✅ 已把在途转换 #{c['id']} 改成按份额：转出 {v:,.2f} 份"
                f"（按最新净值约 ¥{est:,.2f}）。\n"
                f"　转出日 {c['out_date']} 当晚按「份额×当天净值」结算，"
                f"转入份额按确认日净值算。回「转换状态」核对。")

    async def _cancel_conversion(self, member: str, text: str = "") -> str:
        """取消在途转换：可以同时有多笔，「取消转换」撤全部，「取消转换 2」只撤第 2 笔。"""
        active = self._storage.active_conversions(member)
        if not active:
            return "当前没有在途转换。"
        nums = re.findall(r"\d+", text or "")
        if nums and len(active) > 1:
            i = int(nums[0])
            if 1 <= i <= len(active):
                a = active[i - 1]
                self._storage.cancel_conversion_by_id(a["id"])
                return (f"已取消第 {i} 笔：【{a['src_name'] or a['src_code']}】→"
                        f"【{a['tgt_name'] or a['tgt_code']}】（源基金没动，数据无改动）。")
            return f"没有第 {i} 笔，当前在途转换共 {len(active)} 笔。"
        n = self._storage.cancel_active_conversion(member)
        if len(active) > 1:
            return (f"已取消全部 {n} 笔在途转换（源基金一直没动，数据无改动）。\n"
                    f"只想撤其中一笔的话，回「取消转换 序号」，序号按登记顺序。")
        return f"已取消 {n} 笔在途转换（源基金一直没动，数据无改动）。"

    # ---------- 预约确认 ----------

    async def _start_schedule(self, event, umo: str, sender: str) -> str:
        """「预约确认」入口：列出该成员记录，让用户选编号。"""
        member = _norm_member((event.get_sender_name() or "").strip()) or f"成员_{sender}"
        recs = [r for r in self._storage.all_records()
                if self._member_matches(r["member"], member)]
        if not recs:
            return f"📋 {member} 名下暂无记录，请先「导入数据」+截图。"
        self._schedule_select[(umo, sender)] = {
            "member": member, "stage": "choose", "records": recs, "selected": [],
            "prompt_mid": None,
        }
        self._state_ts.setdefault((umo, sender), {})["schedule"] = time.time()
        lines = [f"📋 {member} 的记录："]
        for i, r in enumerate(recs, 1):
            fund = r["fund_name"] or r["fund_code"]
            tag = "（待确认）" if not r["confirmed"] else ""
            lines.append(f"  {i}. {fund}({r['fund_code']}) ¥{r['current']:,.2f} {tag}")
        lines.append("回复数字（空格分隔）选择要预约的条目，例如「1 2 3」；")
        lines.append("也可直接附带时间，如「1 2 3 周四确认」。")
        return "\n".join(lines)

    async def _handle_schedule_select(self, event, umo: str, sender: str) -> str:
        st = self._schedule_select.get((umo, sender))
        text = (event.message_str or "").strip()
        if not st:
            return "预约状态已失效，请重新发「预约确认」。"
        nums = [int(n) for n in re.findall(r"\d+", text)]
        if st["stage"] == "choose":
            recs = st["records"]
            # 一次性理解「编号 + 时间」：支持整体时间或逐条时间
            parsed = await _llm_parse_schedule_input(text, len(recs))
            sel_idx = parsed.get("selected") or []
            times = parsed.get("times") or {}
            if not sel_idx:  # 大模型未识别 → 回退纯数字提取，去掉时间数字
                sel_idx = []
                for n in nums:
                    if 1 <= n <= len(recs) and n not in sel_idx:
                        sel_idx.append(n)
            if not sel_idx:
                return "请回复要预约的条目编号，例如「1 2 3」。"
            sel = [recs[i - 1] for i in sel_idx if 1 <= i <= len(recs)]
            if not sel:
                return f"编号无效，可选范围 1-{len(recs)}。"
            st["selected"] = sel
            if times:
                # 把「原始记录编号」的时间重映射为选中列表内的顺序序号(1..n)，
                # 与 _commit_schedule 的 enumerate(sel,1) 对齐，避免逐条时间错位
                remap: dict = {}
                for k, orig in enumerate(sel_idx, 1):
                    if orig in times:
                        remap[k] = times[orig]
                st["times"] = remap
                dt = next(iter(remap.values()))
                return await self._commit_schedule(st, dt, umo, sender)
            st["stage"] = "time"
            names = "\n".join(f"  {s['fund_name'] or s['fund_code']}({s['fund_code']})" for s in sel)
            return ("已选中 " + "、".join(str(i) for i in sel_idx) + " 条：\n" + names
                    + "\n请回复预约确认时间：可整体如「周四15:00」，也可逐条如「1周四 2周五10点」。")
        else:  # stage == time
            times = await _llm_parse_confirm_times(text, len(st.get("selected", [])))
            if not times:
                dt = await _llm_parse_confirm_time(text)
                if dt:
                    times = {0: dt}
                else:
                    return "没看懂预约时间，请回复如「周四15:00」「明天10点」，或逐条如「1周四 2周五」。"
            if 0 in times:
                st["times"] = {i: times[0] for i in range(1, len(st.get("selected", [])) + 1)}
            else:
                st["times"] = times
            dt = next(iter(times.values()))
            return await self._commit_schedule(st, dt, umo, sender)

    async def _commit_schedule(self, st: dict, dt: datetime, umo: str, sender: str) -> str:
        """把选中的记录设为待确认(入库但不更新)，并按条独立保存各自预约确认时间。"""
        member = st["member"]
        sel = st.get("selected", [])
        times = st.get("times") or {}
        # 仅对待确认记录保持待确认；已确认记录不冻结（有买入挂账的到点转正并入本金）
        for rec in sel:
            if not rec["confirmed"]:
                self._storage.set_pending(member, rec["fund_code"])
        for i, rec in enumerate(sel, 1):
            t = times.get(i) or dt
            self._confirm_schedule[(member, rec["fund_code"])] = t
            self._storage.save_schedule(member, rec["fund_code"], t.strftime("%Y-%m-%d %H:%M"))
        self._schedule_select.pop((umo, sender), None)
        n = len(sel)
        if len({(times.get(i) or dt).strftime("%Y-%m-%d %H:%M")
                for i in range(1, n + 1)}) == 1:
            only = (times.get(1) or dt).strftime("%Y-%m-%d %H:%M")
            time_txt = f"全部在 {only} 自动转正"
        else:
            time_txt = "到点自动转正"
        return (f"⏰ 已预约 {member} 的 {n} 条记录，{time_txt}并更新。\n"
                f"每条记录预约时间独立保存，重启不丢失；预约期间入库但不参与每日更新。")

    @filter.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        try:
            img_path = self._render_help_image()
            if img_path:
                yield event.image_result(img_path)
                return
        except Exception as e:
            logger.error(f"[羊村基金涨跌榜] 帮助图片生成失败: {e}")
        yield event.plain_result(self._help_text())

    def _help_text(self) -> str:
        return (
            "📖 羊村基金涨跌榜 · 使用说明\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "① 查看涨跌榜\n"
            "　@机器人 说「涨跌榜」\n"
            "　→ 输出群内成员理财数据：用户 + 涨跌 + 现资金 + 盈亏\n"
            "② 导入数据\n"
            "　@机器人 说「导入数据」并附持仓截图\n"
            "　→ 自动识别录入成员基金，日期自动取当天\n"
            "　· 若份额不明确，先暂存为【待确认】不参与更新\n"
            "　· 可立即回复各基金金额转正，或回复确认时间预约自动转正，"
            "如「周四确认份额」，到点自动更新\n"
            "　· 也可发「预约确认」：按编号选择记录并指定时间，选中记录入库但不更新，"
            "到点自动转正更新\n"
            "③ 查某人的基金\n"
            "　@张三 基金情况\n"
            "　→ 简洁列出张三持有的基金及当前资金、涨跌、盈亏\n"
            "④ 删除 / 修改数据\n"
            "　@机器人 说「删除基金」→ 列出编号，回复编号即删除对应记录\n"
            "　@机器人 说「修改数据」→ 列出编号，回复「编号 新金额」改本金\n"
            "⑤ 买入基金\n"
            "　@机器人 说「买入基金」/「加仓」/「申购」→ 选择持仓编号 + 金额追加本金，如「1 3000」\n"
            "　也可直接输入新基金名 + 金额，如「招商白酒 5000」\n"
            "　· 买入先入账【待确认】，可附确认时间如「1 3000 周四确认」预约转正\n"
            "⑥ 卖出基金 / 交易明细\n"
            "　@机器人 说「卖出基金」/「赎回」/「减仓」→ 选择编号 + 金额卖出已确认份额，如「1 2000」\n"
            "　·「编号 全部」按当前现值全部卖出（清仓）；待确认份额不受卖出影响\n"
            "　· 卖出即扣减本金并留痕，本次盈亏计入该基金「累计已实现盈亏」，\n"
            "　　「涨跌榜」的总盈亏=浮动盈亏+已实现，清仓后也不会丢\n"
            "　·「交易明细」可查买入/卖出记录\n"
            "　· 直接发买入/卖出交易截图也认：自动读出基金、金额/份额与状态，\n"
            "　　先出确认单，回「确认」才入账；在途（待确认/确认中）先留痕不扣账\n"
            "⑦ 每日自动更新\n"
            f"　每天 {self.cfg.get('daily_update_time', '20:00')} 自动按天天基金最新净值更新成员资金，"
            "并在群内提示「基金已更新」；到预约时间的待确认数据也会自动转正更新\n"
            "⑧ 何时更新（联网分析）\n"
            "　@机器人 说「何时更新」→ 联网分析所有基金当日净值是否已发布、各基金确认份额规则\n"
            "　@张三 何时更新 → 只看张三的持仓\n"
            "⑨ 基金转换\n"
            "　@机器人 说「转换基金」→ 选转出基金（回编号，可带金额：「1 100」转 100 元、「1 50%」转一半）"
            "→ 转入回编号/基金名/6位代码 → 出【转换确认单】\n"
            "　· 确认单自带默认日期：交易日 15:00 前转出日=今天，否则顺延到下一交易日；\n"
            "　　份额确认国内 T+1、QDII T+2，时间取自配置（默认 12:00）\n"
            "　· 回「确认」下单；改日期回「转出日 周五」/「确认时间 周五15:00」；「取消」退出\n"
            "　· 转出日之前源基金继续在榜算涨跌；转出日晚自动按当天净值结算、挂账进转入基金并群内通知\n"
            "　· 转入份额按每笔各自的确认时间转正（同一只基金多笔也不会互相顶掉），历史盈亏完整结转\n"
            "　· 说「转换状态」看进度（在途 + 最近已结算）；「取消转换」撤销，可带序号如「取消转换 2」\n"
            "⑩ 盘中估值（全程图片 + 自动推送）\n"
            "　@机器人 说「盘中估值」→ 出图列出你全部基金当前盘中涨跌 + 持仓加权总预估\n"
            "　· 主动/混合基金用重仓股实时行情估算；指数基金按对应场内ETF行情；仅债券型标 —\n"
            "　· 交易日会自动推送：午间休盘 11:30、下午收盘 15:00（时点可在插件配置里改）\n"
            "⑪ 基金重仓股实时涨跌（全程图片）\n"
            "　@机器人 说「基金持仓」(或「重仓股」) → 发一张选基图 → 回编号 →\n"
            "　出图显示该基金重仓前10股票今日实时涨跌 + 今日盘中走势 + 加权预估今日涨跌\n"
            "　（不发文字列表，全程图片避免刷屏；仅供参考，非准确净值）\n"
            "⑫ 对账 / 大模型改库\n"
            "　@机器人 发【持仓截图】+「对账」→ 大模型读图，和台账逐条比出差异，回「确认」才写入\n"
            "　· 也能直接用嘴改：「对账 永赢医药 本金 500」「对账 016702 现值 205」「对账 删掉 004815」\n"
            "　· 只说哪一栏就只改哪一栏；截图对账会按图里的「持有收益」顺带校正成本\n"
            "　· 对账会自动避开在途转换的基金：若截图里源基金已变少/没了（App 已执行），"
            "回「结算转换」按截图金额立即入账，不会把搬钱当成盈亏\n"
            "　· 转换按份额算：转出份额×转出日净值=金额，金额÷确认日净值=转入份额\n"
            "　· 直接发转换订单截图就认（不用先打「对账」）：自动读转出/转入基金、份额、\n"
            "　　净值与确认日期，出方案后回「确认」才登记；已经在途的用「转换份额 100.8」校正\n"
            "　· 发支付宝「记录详情」的转换订单截图也认：自动读份额换算比例、"
            "按订单的确认日建在途转换，回「确认」落库\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 未识别到基金代码的条目无法自动更新涨跌，"
            "建议截图包含基金代码。\n"
            "💬 除涨跌榜外，本插件的提示消息会自动按时撤回，避免刷屏。\n"
            "📂 数据保存在本地 SQLite：\\.astrbot\\data\\plugin_data\\sheep_fund_rank\\"
        )

    # ---------- 每日自动更新 ----------

    @filter.on_astrbot_loaded()
    async def on_loaded(self, *args, **kwargs) -> None:
        # 先把后台任务拉起来，再做补漏：净值补更新可能要跑几分钟，
        # 串行执行会把开机后紧接着的估值班次（补发窗口 25 分钟）挤过。
        self._ensure_scheduler()
        try:
            await self._estimate_catchup_on_boot()
        except Exception as e:
            logger.error(f"[羊村基金涨跌榜] 开机补发估值失败: {e}")
        try:
            await self._catchup_if_needed()
        except Exception as e:
            logger.error(f"[羊村基金涨跌榜] 加载补更新失败: {e}")

    async def _estimate_scheduler(self) -> None:
        """盘中估值推送的独立调度任务：每 30 秒醒一次对时点，
        与每日净值更新解耦（净值更新慢也不会把 11:30/15:00 的班次顶掉）。"""
        while True:
            try:
                await self._estimate_push_tick(time.localtime())
            except Exception as e:
                logger.error(f"[羊村基金涨跌榜] 盘中估值推送失败: {e}")
            await asyncio.sleep(30)

    def _ensure_scheduler(self) -> None:
        """确保每日更新/预约检查/估值推送的后台任务在跑（单实例，可重复调用）。
        插件热重载不会触发 on_astrbot_loaded，需在 __init__ 里也调用一次。"""
        try:
            if self._timer_task is None or self._timer_task.done():
                self._timer_task = asyncio.create_task(self._daily_scheduler())
                logger.info("[羊村基金涨跌榜] 每日更新/预约检查任务已启动")
            if self._estimate_task is None or self._estimate_task.done():
                self._estimate_task = asyncio.create_task(self._estimate_scheduler())
                try:
                    _en = _plugin_cfg_value("estimate_push_enabled",
                                            self.cfg.get("estimate_push_enabled", True))
                    _sl = self._estimate_push_slots()
                    logger.info(f"[羊村基金涨跌榜] 盘中估值自动推送：{'开启' if _en else '关闭'}，时点 {_sl}")
                except Exception:
                    pass
            logger.info(f"[羊村基金涨跌榜] 桌面组件联动就绪（端口 {_widget_port() or '未运行'}，写库即通知）")
        except RuntimeError:
            # 当前没有运行中的事件循环，交给 on_astrbot_loaded 兜底
            self._timer_task = None
            self._estimate_task = None

    async def _catchup_if_needed(self) -> None:
        """插件加载时补漏：存在已确认记录上次更新早于今天（跨天遗漏）则补一次当日更新。
        周末或法定节假日休市时跳过（当天净值不发布）。"""
        recs = [r for r in self._storage.all_records() if r["confirmed"]]
        if not recs:
            return
        if _game_mode_active():
            logger.info("[羊村基金涨跌榜] 游戏模式开启，跳过加载补更新，退出游戏后由定时任务补。")
            return
        today = time.strftime("%Y-%m-%d")
        if not _is_market_open(today):
            logger.info(f"[羊村基金涨跌榜] 今日 {today} 休市，跳过加载补更新。")
            return
        stale = [r for r in recs if r["last_update"] and r["last_update"] < today]
        if stale:
            logger.info(f"[羊村基金涨跌榜] 检测到 {len(stale)} 条记录未按日更新，加载时自动补一次更新。")
            await self._run_daily_update()

    async def terminate(self) -> None:
        for t in (self._timer_task, self._estimate_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info("[羊村基金涨跌榜] 插件已终止")

    async def _daily_scheduler(self) -> None:
        last_done_day = ""
        last_retry_ts = 0.0
        while True:
            now = time.localtime()
            # 1) 每日自动更新：从 daily_update_time 起按『当日净值发布』跟进，每 40 分钟补一次，
            #    直到所有已确认记录都拿到当日准确净值（QDII 除外，其净值 T+N 滞后用最新净值）。
            uh = getattr(self, "_upd_hour", UPDATE_HOUR)
            um = getattr(self, "_upd_min", UPDATE_MIN)
            today = time.strftime("%Y-%m-%d")
            reached = (now.tm_hour > uh) or (now.tm_hour == uh and now.tm_min >= um)
            # 周末或法定节假日休市：不自动更新（国内基金净值不发布；QDII 下个交易日自动补）
            if reached and today != last_done_day and _is_market_open(today):
                if _game_mode_active():
                    if not getattr(self, "_game_mode_paused_logged", False):
                        logger.info("[羊村基金涨跌榜] 游戏模式开启，暂缓每日净值更新，退出游戏后自动补。")
                        self._game_mode_paused_logged = True
                elif time.time() - last_retry_ts >= 2400:
                    self._game_mode_paused_logged = False
                    last_retry_ts = time.time()
                    try:
                        await self._run_daily_update()
                    except Exception as e:
                        logger.error(f"[羊村基金涨跌榜] 每日更新失败: {e}")
                    if self._all_nav_done_today(today) and not self._qdii_behind_today(today):
                        last_done_day = today
                    elif now.tm_hour >= 23:
                        # 23 点后不再追 QDII 的滞后净值，避免整夜空转；次日自动补
                        last_done_day = today
            # 2) 检查待确认份额的预约是否到期
            try:
                await self._check_schedules()
            except Exception as e:
                logger.error(f"[羊村基金涨跌榜] 预约检查失败: {e}")
            # 3) 盘中估值推送由独立任务 _estimate_scheduler 负责：
            #    每日净值更新可能连跑几分钟，放在同一个循环里会把 11:30/15:00 的班次挤过补发窗口。
            await asyncio.sleep(60)

    async def _check_schedules(self) -> None:
        now_dt = datetime.now()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M")
        # 1) 老式预约（按「成员+基金」一条）
        due = [(m, fc) for (m, fc), dt in self._confirm_schedule.items() if dt <= now_dt]
        by_member: dict[str, list[str]] = {}
        for m, fc in due:
            by_member.setdefault(m, []).append(fc)
        for member, fcs in by_member.items():
            # 某只基金挂着的钱如果全是「转换批次」，确认提示由批次自己负责，这里不重复提示
            pure_lot = []
            for fc in list(fcs):
                lots = self._storage.pending_lots(member=member, fund_code=fc)
                if not lots:
                    continue
                rec = self._storage.get_fund(member, fc)
                lot_sum = round(sum(float(l["amount"] or 0.0) for l in lots), 2)
                pend = round(float((rec or {}).get("pending_amount") or 0.0), 2)
                if pend - lot_sum <= 0.005:
                    pure_lot.append(fc)
            if pure_lot:
                fcs = [fc for fc in fcs if fc not in pure_lot]
                for fc in pure_lot:
                    self._confirm_schedule.pop((member, fc), None)
                    self._storage.remove_schedules(member, fc)
                logger.info(f"[羊村基金涨跌榜] {member} {pure_lot} 的确认已由转换批次接管，跳过重复提示")
                if not fcs:
                    continue
            for fc in fcs:
                self._confirm_schedule.pop((member, fc), None)
                self._storage.remove_schedules(member, fc)
            fund_lines = []
            for fc in fcs:
                nm = fc
                for r in self._storage.all_records():
                    if r["member"] == member and r["fund_code"] == fc:
                        nm = r["fund_name"] or fc
                        break
                fund_lines.append(f"{nm}({fc})")
            notice = (f"⏰ {member} 到确认时间：{'、'.join(fund_lines)}\n"
                      f"回「确认」按最近估值转正，或直接说实际到账金额（「取消确认」保持待确认）。")
            mids = await self._broadcast_no_recall(notice)
            # 记录本次到点的 codes，供用户回复后只转正这几条（不连带转正其它未到点待确认）
            self._pending_confirm[member] = {"mids": mids, "codes": fcs}
            logger.info(f"[羊村基金涨跌榜] {member} {len(fcs)} 条预约到点，已提示待确认结果 (群数 {len(mids)})")

        # 2) 转换转入的「批次」：每笔自己一个确认时间，到点各自提示、各自转正
        due_lots = [l for l in self._storage.lots_due(now_str) if not l["notified"]]
        if not due_lots:
            return
        lots_by_member: dict[str, list[dict]] = {}
        for lot in due_lots:
            lots_by_member.setdefault(lot["member"], []).append(lot)
        for member, lots in lots_by_member.items():
            lines = [f"{l['fund_code']} ¥{l['amount']:,.2f}" for l in lots]
            notice = (f"⏰ {member} {len(lots)} 笔转换份额到确认时间：{'、'.join(lines)}\n"
                      f"回「确认」按各自的金额转正，或直接说实际到账金额。")
            mids = await self._broadcast_no_recall(notice)
            for lot in lots:
                self._storage.mark_lot_notified(lot["id"])
            slot = self._pending_confirm.setdefault(member, {"mids": [], "codes": []})
            slot["mids"] = list(slot.get("mids") or []) + list(mids or [])
            merged = list(slot.get("codes") or [])
            for l in lots:
                if l["fund_code"] not in merged:
                    merged.append(l["fund_code"])
            slot["codes"] = merged
            logger.info(f"[羊村基金涨跌榜] {member} {len(lots)} 笔转换批次到点，已提示确认 (群数 {len(mids or [])})")

    def _plugin_group_gid(self) -> str:
        """广播目标群：优先配置 push_group_openid，否则取最近活跃的群。
        写死群号在群被注销/换群后会静默发失败，故改为动态解析。"""
        gid = str(self.cfg.get("push_group_openid", "") or "").strip()
        if not gid:
            gid = str(_plugin_cfg_value("push_group_openid", "") or "").strip()
        if gid:
            return gid
        umo = _last_active_group_umo()
        if umo:
            g = self._group_openid_from_umo(umo)
            if g:
                return g
        return ""

    def _group_targets(self, only_gid: str = "") -> list[tuple[str, list[str]]]:
        """按「群 openid」归并发送目标：同一个群可能有多个 umo（多个机器人账号在同一群），
        只发一次，否则群里会收到重复消息。返回 [(gid, [umo, ...])]，umo 按顺序做失败兜底。"""
        out: dict[str, list[str]] = {}
        for umo in _group_umos():
            gid = self._group_openid_from_umo(umo)
            if not gid:
                continue
            if only_gid and gid != only_gid:
                continue
            bucket = out.setdefault(gid, [])
            if umo not in bucket:
                bucket.append(umo)
        return list(out.items())

    async def _broadcast_no_recall(self, text: str) -> list[dict]:
        """广播一条提示到所有群，不自动撤回；返回 [{umo,gid,mid}] 供后续手动撤回。
        优先走 QQ 官方 bot api（mid 为真 QQ message_id，可被 botpy 撤回），
        未缓存 bot api 时降级为 HTTP im/messages（mid 不一定可撤）。
        每个群只发一次（同群多账号 umo 只做失败兜底）。"""
        # 广播目标：只发当前在用的那个群（动态解析，旧群注销后不再静默失败）
        targets = self._group_targets(self._plugin_group_gid())
        if not targets:
            return []
        out = []
        if self._bot_api is not None:
            for gid, umos in targets:
                for umo in umos:
                    try:
                        ret = await self._bot_api.post_group_message(
                            group_openid=gid, content=text, msg_type=0)
                        mid = self._extract_mid(ret)
                        out.append({"umo": umo, "gid": gid, "mid": mid})
                        break  # 该群已发出，不再用第二个账号重复发
                    except Exception as e:
                        logger.warning(f"[羊村基金涨跌榜] 广播发送失败 {umo}: {e}")
            return out
        cfg = _load_cmd_config()
        if not cfg:
            return []
        dash = cfg.get("dashboard") or {}
        secret = dash.get("jwt_secret")
        username = dash.get("username") or "astrbot"
        if not secret:
            return []
        token = _make_jwt(secret, username)
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            for gid, umos in targets:
                for umo in umos:
                    sent = False
                    for attempt in range(2):
                        r = None
                        try:
                            r = await client.post(
                                f"http://127.0.0.1:6185/api/v1/im/messages",
                                headers={"Authorization": f"Bearer {token}"},
                                json={"message": text, "umo": umo})
                        except Exception:
                            r = None
                        if r is not None and r.status_code < 300:
                            # 先认定「已发出」再解析 mid：解析失败绝不能触发重发（会重复刷屏）
                            sent = True
                            mid = None
                            try:
                                mid = self._extract_http_mid(r)
                            except Exception:
                                mid = None
                            out.append({"umo": umo, "gid": gid, "mid": mid})
                            break
                        await asyncio.sleep(0.6)
                    if sent:
                        break  # 该群已发出，不再用第二个账号重复发
                await asyncio.sleep(0.4)
        return out

    async def _cancel_pending_confirm(self, member: str) -> None:
        """撤回该成员所有到点提示并清空待确认结果等待状态。"""
        items = (self._pending_confirm.pop(member, None) or {}).get("mids") or []
        # 提示已撤回：把批次重置为未提示，下一轮到点检查会重新提醒
        try:
            self._storage.reset_lot_notified(member)
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 重置批次提示状态失败 {member}: {e}")
        if not items or not self._bot_api:
            return
        for it in items:
            mid = it.get("mid")
            if not mid:
                continue
            try:
                await self._recall_after(self._bot_api, it["gid"], str(mid), 0, "group")
            except Exception as e:
                logger.warning(f"[羊村基金涨跌榜] 撤回 {member} 到点提示失败 {it.get('gid')}/{mid}: {e}")

    async def _recall_prompt_mid(self, gid: str, mid) -> None:
        """立即撤回一条机器人消息（用于预约确认选择提示）。"""
        if not mid or not self._bot_api:
            return
        try:
            await self._recall_after(self._bot_api, gid, str(mid), 0,
                                     getattr(self, "_send_mode", "group"))
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 撤回提示失败 {gid}/{mid}: {e}")

    def _match_pending_confirm_member(self, sender, mname: str) -> str | None:
        """在待确认结果等待名单中匹配当前用户对应的成员名。"""
        cands = [mname]
        qq = str(sender)
        for name, q in GROUP_MEMBERS.items():
            if str(q) == qq:
                cands.append(name)
                break
        for c in cands:
            if c and c in self._pending_confirm:
                return c
        return None

    async def _cancel_reserve(self, member: str) -> str:
        """取消该成员的预约计划与/或确认等待，数据保持待确认。"""
        if not member:
            return "请@机器人说明要取消谁的预约确认。"
        removed = []
        has_sched = any(m == member for (m, _fc) in self._confirm_schedule)
        if has_sched:
            for key in list(self._confirm_schedule):
                if key[0] == member:
                    del self._confirm_schedule[key]
            self._storage.remove_schedules(member)
            removed.append("预约计划")
        if member in self._pending_confirm:
            await self._cancel_pending_confirm(member)
            removed.append("确认等待")
        if not removed:
            return f"{member} 没有正在生效的预约或确认等待，无需取消。"
        return f"已取消 {member} 的{'、'.join(removed)}，数据保持待确认。"

    async def _confirm_and_update(self, member: str) -> int:
        """把该成员『已到预约确认时间』的待确认记录转正，只并入本金，不立即应用涨跌。
        涨跌统一交给每日自动更新（国内基金按确认日当晚净值、QDII 按自身最新净值日）计算，
        避免确认瞬间就被喂入旧净值涨跌。返回转正行数。
        转换批次按每笔各自的确认时间与金额分别转正，不会互相牵连。"""
        pcs = self._pending_confirm.get(member, {})
        codes = pcs.get("codes") or self._storage.member_codes(member)
        if not codes:
            return 0
        now_str = time.strftime("%Y-%m-%d %H:%M")
        n = 0
        for lot in self._storage.lots_due(now_str, member=member, codes=codes):
            # 按份额转换：确认时用「确认日净值」把金额换成转入份额；净值还没出就用最新净值顶上，
            # 份额也会被钉住，下次净值更新直接 份额×净值，不会把这段涨跌吃掉。
            if float(lot.get("shares") or 0.0) <= 0 and float(lot.get("amount") or 0.0) > 0:
                nav_in = _fetch_nav_on(lot["fund_code"], (lot.get("confirm_at") or "")[:10])
                if not nav_in:
                    latest = _fetch_latest_nav(lot["fund_code"])
                    nav_in = latest[1] if latest else None
                if nav_in:
                    self._storage.set_lot_shares(
                        lot["id"], float(lot["amount"]) / nav_in, nav_in)
            n += self._storage.confirm_lot(lot["id"])
        n += self._storage.confirm_member_codes(member, codes)
        return n

    async def _do_update(self) -> tuple[int, int]:
        """执行一次基金更新：只更新已确认(confirmed=1)持仓，完全跳过待确认。
        返回 (total, waiting)：total=已更新条数，waiting=当日净值未发布暂缓的条数。"""
        codes = self._storage.distinct_codes()
        if not codes:
            return 0, 0
        today = time.strftime("%Y-%m-%d")
        total = 0
        waiting = 0
        # 先把净值并发抓下来（网络 I/O 不占锁），再进锁一次性写库：
        # 否则整轮净值更新期间，用户在群里发指令都会被锁住排队。
        async def _one(code):
            try:
                return code, await asyncio.to_thread(_fetch_nav_full, code)
            except Exception:
                return code, ("", None, 0.0, code)

        fetched = await asyncio.gather(*[_one(c) for c in codes])
        async with self._storage._lock:
            for code, (nav_date, change, nav, name) in fetched:
                if change is None or not nav_date:
                    continue
                # 自动补漏：只要抓到的是比该基金 last_update 更新的净值日就更新
                # （update_fund_day 内部已用 last_update<>nav_date 与确认日 防重、防写旧值）
                total += self._storage.update_fund_day(code, change, nav_date, nav=nav)
        # 在途转换结算：转出日净值已落地的，按当天市值清零源、挂账转入
        settled = await asyncio.to_thread(self._settle_conversions_sync)
        self._last_settle_notice = settled
        return total, waiting

    def _settle_one(self, c: dict, amount: float | None = None,
                    basis: float | None = None) -> str | None:
        """结算一笔在途转换，返回群提示文本；不满足条件返回 None。
        amount 给定时按「实际到账额」覆盖（对账/App 已执行时用），否则按计划或整只。"""
        src = self._storage.get_fund(c["member"], c["src_code"])
        if not src or not src["confirmed"]:
            return None
        if amount is None and (src["last_update"] or "") < c["out_date"]:
            return None  # 转出日净值还没出来，等下一轮
        full = round(float(src["current"] or 0.0), 2)
        if full <= 0:
            self._storage.mark_conversion_settled(c["id"], 0.0)
            return None
        # 按份额转换优先：转出份额 × 转出日净值 = 转出金额，份额按申请的份数精确扣。
        # 老记录（按金额/比例登记）保持原逻辑，不打断它们在途那笔。
        plan = float(c.get("part_amount") or 0.0)
        ratio = float(c.get("part_ratio") or 0.0)
        out_shares = float(c.get("out_shares") or 0.0)
        shares_total = float(src.get("shares") or 0.0)
        src_shares_out = None
        nav_out = 0.0
        whole = False
        if out_shares > 0 and shares_total > 0:
            whole = out_shares >= shares_total - 0.0001
            src_shares_out = None if whole else out_shares
            ratio = min(round(out_shares / shares_total, 6), 1.0)
            if whole:
                ratio = 0.0          # 份额全转 = 整只转出，金额按现值走，避免净值/份额不同步时少算
            elif amount is None:
                got = _fetch_nav_on(c["src_code"], c["out_date"])
                nav_out = float(got or 0.0)
                amount = (round(out_shares * nav_out, 2) if nav_out > 0
                          else round(full * ratio, 2))
        if amount is None:
            if 0 < ratio < 1:
                amount = round(full * ratio, 2)
            else:
                amount = round(min(plan, full), 2) if 0 < plan < full else full
        else:
            amount = round(min(max(float(amount), 0.0), full), 2)
        if amount <= 0:
            self._storage.mark_conversion_settled(c["id"], 0.0)
            return None
        if basis is None:
            basis = round(float(src["principal"] or 0.0) * (amount / full), 2)
        else:
            basis = round(min(float(basis), float(src["principal"] or 0.0)), 2)
        zero_src = amount >= full - 0.005
        # 转入份额 = 转入金额 ÷ 转入确认日净值；确认日净值还没出来就先不算，
        # 等「到点确认」时再用当天净值补（_confirm_and_update 里做）。
        in_nav = float(c.get("in_nav") or 0.0)
        in_shares = float(c.get("in_shares") or 0.0)
        _cd = (c.get("confirm_at") or "")[:10]
        if in_shares > 0:
            pass  # 截图里已经给了确认份额，直接用
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", _cd):
            _nav_in = _fetch_nav_on(c["tgt_code"], _cd)
            if _nav_in:
                in_nav = _nav_in
                in_shares = round(amount / _nav_in, 4)
        tgt_before = self._storage.get_fund(c["member"], c["tgt_code"])
        self._storage.settle_conversion_rows(
            c["member"], c["src_code"], c["tgt_code"], c["tgt_name"], amount, basis,
            zero_src=zero_src, src_shares_out=src_shares_out)
        self._storage.add_trade_log(c["member"], c["src_code"], c["src_name"], "卖出", amount)
        self._storage.add_trade_log(c["member"], c["tgt_code"], c["tgt_name"], "买入", amount)
        if tgt_before is None or tgt_before["confirmed"]:
            # 钱落进 pending_amount（转入基金原本没有、或已确认持有）→ 按「批次」记自己的确认时间，
            # 两笔转到同一只基金也不会互相覆盖确认时间、也不会被一起提前转正。
            self._storage.add_pending_lot(
                c["member"], c["tgt_code"], c["tgt_name"] or "",
                amount, basis, c["confirm_at"], c["id"],
                shares=in_shares, nav=in_nav)
            # 转入基金原本没有别的挂账 → 老的「成员+基金」预约已被批次取代，清掉免得到点双提示
            if (tgt_before is None
                    or float(tgt_before.get("pending_amount") or 0.0) <= 0.005):
                self._confirm_schedule.pop((c["member"], c["tgt_code"]), None)
                self._storage.remove_schedules(c["member"], c["tgt_code"])
        elif c["confirm_at"]:
            # 转入记录本身就是待确认的（钱已直接并入它的本金）→ 仍用它自己的整条预约
            try:
                dt = datetime.strptime(c["confirm_at"], "%Y-%m-%d %H:%M")
                self._confirm_schedule[(c["member"], c["tgt_code"])] = dt
                self._storage.save_schedule(c["member"], c["tgt_code"], c["confirm_at"])
            except ValueError:
                pass
        self._storage.mark_conversion_settled(c["id"], amount, in_nav, in_shares)
        part_note = "" if zero_src else f"（源基金保留 ¥{full - amount:,.2f}）"
        if out_shares > 0:
            if whole:
                head = f"整只转出 {out_shares:,.2f} 份"
            elif nav_out > 0:
                head = f"转出 {out_shares:,.2f} 份 × 净值 {nav_out:.4f}"
            else:
                head = f"转出 {out_shares:,.2f} 份（占持仓 {ratio * 100:.1f}%）"
            detail = f"{head} = ¥{amount:,.2f}"
            if in_shares > 0:
                detail += f" → 转入 {in_shares:,.2f} 份（净值 {in_nav:.4f}）"
            else:
                detail += " → 转入份额待确认日净值出炉后并入"
        else:
            detail = f"¥{amount:,.2f}"
        return (f"🔄 {c['member']} 转换结算 {c['src_code']}→{c['tgt_code']} "
                f"{detail}{part_note}，{c['confirm_at'] or '稍后'} 确认"
                f"（按转出日净值，未扣转换费）")

    def _settle_conversions_sync(self) -> list[str]:
        """同步结算在途转换：源基金 last_update >= out_date 时按当天 current 结算。"""
        out: list[str] = []
        for c in self._storage.active_conversions():
            n = self._settle_one(c)
            if n:
                out.append(n)
        return out

    def _estimate_push_slots(self) -> list[str]:
        """读取配置里的估值推送时点（HH:MM 逗号分隔），默认午间休盘 11:30 + 收盘 15:00。"""
        raw = str(_plugin_cfg_value("estimate_push_times",
                                    self.cfg.get("estimate_push_times", "11:30,15:00")) or "")
        out: list[str] = []
        for seg in raw.replace("，", ",").split(","):
            seg = seg.strip()
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", seg)
            if not m:
                continue
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                slot = f"{h:02d}:{mi:02d}"
                if slot not in out:
                    out.append(slot)
        return out

    @staticmethod
    def _estimate_slot_label(slot: str) -> str:
        """按推送时点给一个说法：午休前后→午间休盘，收盘前后→今日收盘，其余→盘中。"""
        try:
            h, m = map(int, slot.split(":"))
            mins = h * 60 + m
        except Exception:
            return "盘中估值"
        if 11 * 60 + 20 <= mins <= 11 * 60 + 45:
            return "午间休盘"
        if 14 * 60 + 55 <= mins <= 15 * 60 + 20:
            return "今日收盘"
        if 15 * 60 + 45 <= mins <= 16 * 60 + 20:
            return "港股收盘"
        if 7 * 60 + 45 <= mins <= 8 * 60 + 25:
            return "隔夜美股"
        return "盘中估值"

    @staticmethod
    def _is_after_close(now=None) -> bool:
        """A股是否已收盘：交易日且当前时间 >= 15:00。"""
        now = now or time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        if not _is_market_open(today):
            return False
        return (now.tm_hour, now.tm_min) >= (15, 0)

    def _save_close_snapshot(self, rows: list[dict]) -> None:
        """收盘后锁定一份估值快照，避免账目变动/重复取数让群里数字继续漂。"""
        try:
            self._storage.set_meta("estimate_close_snapshot", json.dumps(
                {"date": time.strftime("%Y-%m-%d"), "at": time.strftime("%H:%M"),
                 "rows": rows}, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 保存收盘快照失败: {e}")

    def _load_close_snapshot(self) -> dict | None:
        try:
            raw = self._storage.get_meta("estimate_close_snapshot") or ""
            if not raw:
                return None
            data = json.loads(raw)
            if data.get("date") != time.strftime("%Y-%m-%d"):
                return None
            return data
        except Exception:
            return None

    def _collect_estimate_rows(self) -> list[dict]:
        """汇总全体成员当前盘中估算（同步取行情，调用方放线程里跑）。
        只统计已确认且有现值的记录；同一只基金被多人持有时复用同一次估算结果。"""
        agg: dict[str, dict] = {}
        for r in self._storage.all_records():
            if not r["confirmed"]:
                continue
            if (r["current"] or 0.0) <= 0.005:
                continue
            m = agg.setdefault(r["member"], {"current": 0.0, "funds": []})
            m["current"] += r["current"] or 0.0
            m["funds"].append(r)
        # 在途卖出（已冻结、未确认）：App 里还挂在持仓、行情仍在动，
        # 估值行照常显示并标「卖在途」，不能因为台账清零就把它从预估里抹掉。
        try:
            for p in self._storage.pending_sells(status="pending"):
                exp = float(p.get("expected") or 0.0)
                if exp <= 0.005:
                    continue
                m = agg.setdefault(p["member"], {"current": 0.0, "funds": []})
                m["current"] += exp
                m["funds"].append({
                    "fund_code": p["fund_code"],
                    "fund_name": p["fund_name"] or p["fund_code"],
                    "current": exp, "confirmed": 1, "in_sell": True,
                })
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 在途卖出并入估值失败: {e}")
        codes = list(dict.fromkeys(r["fund_code"] for m in agg.values() for r in m["funds"]))
        try:
            est_map = self._fetch_estimates_bulk(codes)
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 批量估算失败，逐只回退: {e}")
            est_map = {}
        if not est_map:
            est_map = {c: self._fetch_fund_estimate(c) for c in codes}
        rows: list[dict] = []
        preopen = _a_share_preopen()
        # 海外基金：白天美股没开，用「昨夜美股收盘那一场」的涨跌顶上，并在明细里标注
        us_idx = {}
        try:
            has_overseas = any(self._is_overseas_fund(r["fund_name"] or "")
                               for m in agg.values() for r in m["funds"])
            if has_overseas:
                us_idx = self._fetch_us_indices()
        except Exception:
            us_idx = {}
        for member, m in agg.items():
            num = den = 0.0
            on_amt = 0.0  # 昨夜美股贡献金额（白天板单独标出，让用户看到昨晚那部分算进去了）
            items: list[dict] = []
            for r in m["funds"]:
                code = r["fund_code"]
                est_t = est_map.get(code) or (None, 0.0, "none", code)
                est = est_t[0]
                kind = est_t[2] if len(est_t) > 2 else "none"
                overnight = False
                if self._is_overseas_fund(r["fund_name"] or "") and not preopen:
                    o = self._overnight_est(code, r["fund_name"] or "", kind, us_idx)
                    if o is not None:
                        est, overnight = o, True
                # A股还没开盘时，ETF/个股报价还是「昨收/0」，不能当 0.00 盘面显示
                if preopen and kind in ("etf", "stock") and est is not None:
                    est = None
                cur = r["current"] or 0.0
                items.append({
                    "fund_code": code,
                    "fund_name": r["fund_name"] or code,
                    "current": round(cur, 2),
                    "est": est,
                    "kind": kind,
                    "overnight": overnight,
                    "in_sell": bool(r.get("in_sell")),
                })
                if est is not None and cur > 0:
                    num += cur * est
                    den += cur
                    if overnight:
                        on_amt += cur * est / 100.0
            items.sort(key=lambda x: (x["est"] is None, -(x["est"] or 0.0)))
            # 盘前 A股没盘面，加权总预估没有意义 → 显示「—」而不是假的 0.00
            total = None if preopen else (round(num / den, 2) if den else None)
            rows.append({
                "member": member,
                "current": round(m["current"], 2),
                "est": total,
                "amount": round(num / 100.0, 2) if den else None,
                "overnight_amount": round(on_amt, 2),
                "funds": len(m["funds"]),
                "items": items,
            })
        # 有盘面按估算降序在前，无盘面（纯债/货币）排后
        rows.sort(key=lambda x: (x["est"] is None, -(x["est"] or 0.0)))
        return rows

    def _render_estimate_board_image(self, rows: list[dict], title_extra: str,
                                     pending_total: float = 0.0,
                                     note: str = "", overnight: bool = False) -> str | None:
        """渲染全员盘中估值图：每行一个成员（现资金 / 预估盈亏金额 / 盘中估算）。
        rows: [{member,current,est,amount}]，est=None 表示无个股盘面（纯债/货币）。
        note: 追加的说明行（如盘前 A50 期货参考），为空则忽略。"""
        from PIL import Image, ImageDraw, ImageFont
        font_path = self._find_font()
        if not font_path:
            return None
        title_f = ImageFont.truetype(font_path, 32)
        sub_f = ImageFont.truetype(font_path, 16)
        txt_f = ImageFont.truetype(font_path, 20)
        name_f = ImageFont.truetype(font_path, 22)
        big_f = ImageFont.truetype(font_path, 27)
        fund_f = ImageFont.truetype(font_path, 17)
        sm_f = ImageFont.truetype(font_path, 14)
        W, pad = 900, 34
        head_h, row_h, sub_h, foot_h = 100, 64, 34, 96
        n_sub = sum(len(r.get("items") or []) for r in rows)
        extra = 26 if (pending_total and pending_total > 0.005) else 0
        note_h = 24 if note else 0
        H = head_h + row_h * (len(rows) + 1) + sub_h * n_sub + foot_h + 12 + extra + note_h
        img = Image.new("RGB", (W, H), "#FFFFFF")
        d = ImageDraw.Draw(img)
        up, down, flat = "#D93B34", "#0E9F6E", "#8A90A0"
        # 顶部标题条
        d.rectangle([0, 0, W, head_h], fill=(38, 50, 110))
        d.text((pad, 20), "持仓预估涨跌 · 全天汇总" if (title_extra and title_extra.startswith(
            ("今日收盘", "港股收盘", "隔夜美股"))) else "盘中估值 · 持仓预估涨跌",
               font=title_f, fill="#FFFFFF")
        d.text((pad, 64), title_extra, font=sub_f, fill="#C8D0F0")
        y = head_h
        x_member = pad + 44
        x_pct, x_amt, x_cur = W - pad, W - pad - 170, W - pad - 330
        d.text((pad, y + 20), "#", font=txt_f, fill="#8A90A0")
        d.text((x_member, y + 20), "成员", font=txt_f, fill="#8A90A0")
        d.text((x_cur, y + 20), "现资金", font=txt_f, fill="#8A90A0", anchor="ra")
        d.text((x_amt, y + 20), "预估盈亏", font=txt_f, fill="#8A90A0", anchor="ra")
        d.text((x_pct, y + 20), "盘中估算" if (not title_extra or title_extra.startswith("盘中"))
               else "估算涨跌", font=txt_f, fill="#8A90A0", anchor="ra")
        y += row_h
        for i, r in enumerate(rows, 1):
            if i % 2 == 0:
                d.rectangle([0, y, W, y + row_h], fill="#F7F9FF")
            d.text((pad, y + 20), str(i), font=txt_f, fill="#5B6270")
            d.text((x_member, y + 17), r["member"], font=name_f, fill="#20242E")
            d.text((x_cur, y + 19), f"¥{r['current']:,.2f}", font=txt_f,
                   fill="#5B6270", anchor="ra")
            if r["est"] is None:
                d.text((x_amt, y + 19), "—", font=txt_f, fill=flat, anchor="ra")
                d.text((x_pct, y + 16), "—", font=big_f, fill=flat, anchor="ra")
            else:
                amt = r["amount"] or 0.0
                acol = up if amt > 0 else (down if amt < 0 else flat)
                pcol = up if r["est"] > 0 else (down if r["est"] < 0 else flat)
                d.text((x_amt, y + 19), f"{'+' if amt >= 0 else '-'}¥{abs(amt):,.2f}",
                       font=txt_f, fill=acol, anchor="ra")
                d.text((x_pct, y + 16), f"{r['est']:+.2f}%", font=big_f, fill=pcol, anchor="ra")
            on = r.get("overnight_amount") or 0.0
            if abs(on) > 0.005:
                # 白天板：把昨夜美股贡献单独标出来，让用户看到昨晚那部分已经算进预估
                d.text((pad + 44, y + 39),
                       f"昨夜美股贡献 {'+' if on >= 0 else '-'}¥{abs(on):,.2f}（已计入）",
                       font=sm_f, fill="#C07A2B")
            y += row_h
            # 该成员持仓逐只明细（这样每个人自己买的基金都能看到）
            for it in r.get("items") or []:
                d.text((pad + 44, y + 10), it["fund_name"], font=fund_f, fill="#6B7280")
                if it.get("in_sell"):
                    # 卖出在途：份额已冻结但还没确认，App 仍算持仓，标出来免得误以为丢了
                    d.text((pad + 44 + fund_f.getlength(it["fund_name"]) + 8, y + 11),
                           "卖在途", font=sm_f, fill="#C07A2B")
                d.text((x_cur, y + 11), f"¥{it['current']:,.2f}", font=fund_f,
                       fill="#9AA0B0", anchor="ra")
                if it["est"] is None:
                    d.text((x_pct, y + 10), "—", font=fund_f, fill=flat, anchor="ra")
                else:
                    c = up if it["est"] > 0 else (down if it["est"] < 0 else flat)
                    d.text((x_pct, y + 10), f"{it['est']:+.2f}%", font=fund_f, fill=c, anchor="ra")
                    if it.get("overnight"):
                        # 美股白天休市，这个数是「昨夜美股收盘那一场」，标出来免得和当天 A股 混算
                        d.text((x_pct - 78, y + 12), "昨夜", font=sm_f, fill="#C07A2B", anchor="ra")
                y += sub_h
        d.line([(pad, y + 6), (W - pad, y + 6)], fill="#E6E9F2", width=1)
        d.text((pad, y + 20),
               ("海外基金按昨夜美股收盘估算 · 指数型 QDII 直接取对应指数涨幅" if overnight
                else "估算=最新季报重仓股实时加权 · 指数基金按对应场内ETF近似"),
               font=sm_f, fill="#9AA0B0")
        d.text((pad, y + 44),
               ("— 表示无盘面 · QDII 净值公布一般比场内行情晚 1 个交易日" if overnight
                else "— 表示无盘面 · 标「昨夜」的是美股刚收盘那一场（白天美股休市）"),
               font=sm_f, fill="#9AA0B0")
        if pending_total and pending_total > 0.005:
            d.text((pad, y + 64), f"另有在途买入 ¥{pending_total:,.2f} 确认中，暂未计入", font=sm_f, fill="#C07A2B")
            d.text((pad, y + 84), "更新于 " + time.strftime("%Y-%m-%d %H:%M"), font=sm_f, fill="#B0B4C0")
        else:
            d.text((pad, y + 68), "更新于 " + time.strftime("%Y-%m-%d %H:%M"), font=sm_f, fill="#B0B4C0")
        if note:
            d.text((pad, y + 92), note, font=sm_f, fill="#C07A2B")
        out = os.path.join(str(_db_path().parent), "estimate_board_tmp.png")
        img.save(out, "PNG")
        return out

    async def _broadcast_image_async(self, path: str) -> bool:
        """主动把图片推到目标群：先 POST /api/v1/file 拿 attachment_id，再按消息链发送。
        QQ 官方没有「直接给图片路径」的发送接口，必须走这一跳。"""
        if not path or not os.path.exists(path):
            return False
        targets = self._group_targets(self._plugin_group_gid())
        if not targets:
            logger.warning("[羊村基金涨跌榜] 图片推送失败：未找到目标群")
            return False
        cfg = _load_cmd_config()
        if not cfg:
            return False
        dash = cfg.get("dashboard") or {}
        secret = dash.get("jwt_secret")
        if not secret:
            return False
        token = _make_jwt(secret, dash.get("username") or "astrbot")
        sent = False
        async with httpx.AsyncClient(verify=False, timeout=60) as client:
            try:
                with open(path, "rb") as f:
                    up = await client.post(
                        "http://127.0.0.1:6185/api/v1/file",
                        headers={"Authorization": f"Bearer {token}"},
                        files={"file": (os.path.basename(path), f, "image/png")})
                if up.status_code >= 300:
                    logger.warning(f"[羊村基金涨跌榜] 图片上传失败 {up.status_code}: {up.text[:200]}")
                    return False
                aid = (up.json().get("data") or {}).get("attachment_id")
                if not aid:
                    logger.warning("[羊村基金涨跌榜] 图片上传未返回 attachment_id")
                    return False
            except Exception as e:
                logger.warning(f"[羊村基金涨跌榜] 图片上传异常: {e}")
                return False
            for gid, umos in targets:
                for umo in umos:
                    for attempt in range(2):
                        try:
                            r = await client.post(
                                "http://127.0.0.1:6185/api/v1/im/messages",
                                headers={"Authorization": f"Bearer {token}"},
                                json={"umo": umo,
                                      "message": [{"type": "image", "attachment_id": aid}]})
                        except Exception as e:
                            logger.warning(f"[羊村基金涨跌榜] 图片发送异常 {umo}: {e}")
                            r = None
                        if r is not None and r.status_code < 300:
                            sent = True
                            logger.info(f"[羊村基金涨跌榜] 估值图已发送 {umo}")
                            break
                        if r is not None:
                            logger.warning(f"[羊村基金涨跌榜] 图片发送失败 {umo} "
                                           f"{r.status_code}: {r.text[:160]}")
                        await asyncio.sleep(0.6)
                    if sent:
                        break  # 该群已发出，不再用第二个账号重复发
        return sent

    async def _push_intraday_estimate(self, slot: str) -> None:
        """把全体成员的盘中估算汇总成一条消息推到羊村群。"""
        if _game_mode_active():
            logger.info("[羊村基金涨跌榜] 游戏模式开启，跳过盘中估值推送。")
            return
        label = self._estimate_slot_label(slot)
        try:
            rows = await asyncio.to_thread(self._collect_estimate_rows)
        except Exception as e:
            logger.error(f"[羊村基金涨跌榜] 盘中估值取数失败: {e}")
            return
        if not rows:
            logger.info("[羊村基金涨跌榜] 盘中估值无有效持仓，跳过推送。")
            return
        lines = [(f"📈 盘中估值 · {label} {slot}" if label != "盘中估值"
                  else f"📈 盘中估值 {slot}"), "━━━━━━━━━━━━"]
        # 08:00 这班是「隔夜美股」：A股还没开，只讲海外持仓 + 昨夜三大指数
        overnight = slot.strip().startswith("08") or label == "隔夜美股"
        # 白天班也取昨夜三大指数，让用户看到昨晚美股那部分被算进预估
        try:
            us_idx = self._fetch_us_indices()
        except Exception:
            us_idx = {}
        show_rows = rows
        idx_note = ""
        if us_idx:
            idx_note = "昨夜 " + " · ".join(
                f"{k} {v[0]:+.2f}%" for k, v in us_idx.items())
        if overnight:
            try:
                us_rows = await asyncio.to_thread(self._build_overseas_rows, rows)
            except Exception as e:
                logger.warning(f"[羊村基金涨跌榜] 海外持仓汇总失败: {e}")
                us_rows = []
            if us_rows:
                show_rows = us_rows
                label = "隔夜美股"
        for r in show_rows:
            if r["est"] is None:
                lines.append(f"👤 {r['member']}  —  现资金 ¥{r['current']:,.2f}")
            else:
                amt = r["amount"] or 0.0
                on = r.get("overnight_amount") or 0.0
                on_txt = (f"（昨夜美股 {'+' if on >= 0 else '-'}¥{abs(on):,.2f}）"
                          if abs(on) > 0.005 else "")
                lines.append(
                    f"👤 {r['member']}  {r['est']:+.2f}%  "
                    f"预估 {'+' if amt >= 0 else '-'}¥{abs(amt):,.2f}{on_txt}  "
                    f"现资金 ¥{r['current']:,.2f}")
        lines.append("━━━━━━━━━━━━")
        if idx_note:
            lines.append(idx_note)
        a50_note = ""
        if _a_share_preopen() and not overnight:
            a50 = self._fetch_a50_change()
            if a50:
                a50_note = f"盘前参考：A50 期货 {a50[0]:+.2f}%（{a50[2]}）"
                lines.append(a50_note)
        if overnight:
            lines.append("海外基金按美股昨夜收盘估算 · 与基金公司公布的 QDII 净值仍有延迟差")
        else:
            lines.append("估算=最新季报重仓股实时加权 · 指数基金按场内 ETF 近似 · 仅供参考")
        # 优先发图：群里看起来直观，也不刷屏；渲染/发送失败再退回文字
        img_path = None
        try:
            title_extra = (f"{label} {slot}" if label != "盘中估值" else f"盘中 {slot}")
            try:
                pend_total = sum(float(r.get("pending_amount") or 0)
                                 for r in self._storage.all_records())
            except Exception:
                pend_total = 0.0
            board_note = idx_note or a50_note
            img_path = await asyncio.to_thread(
                self._render_estimate_board_image, show_rows, title_extra, pend_total,
                board_note, overnight)
        except Exception as e:
            logger.error(f"[羊村基金涨跌榜] 估值图渲染失败: {e}")
        if img_path:
            try:
                if await self._broadcast_image_async(img_path):
                    logger.info(f"[羊村基金涨跌榜] 盘中估值图 {label} {slot} 已推送，成员 {len(rows)}")
                    if self._is_after_close() and (label == "今日收盘" or self._load_close_snapshot() is None):
                        self._save_close_snapshot(rows)
                    _notify_widget("盘中估值")
                    return
            except Exception as e:
                logger.error(f"[羊村基金涨跌榜] 估值图推送失败: {e}")
        sent = await self._broadcast_no_recall("\n".join(lines))
        logger.info(f"[羊村基金涨跌榜] 盘中估值 {label} {slot} 已推送 {len(sent)} 个群，成员 {len(rows)}")
        if sent and self._is_after_close() and (label == "今日收盘" or self._load_close_snapshot() is None):
            self._save_close_snapshot(rows)
        _notify_widget("盘中估值")

    def _push_done_load(self) -> set[str]:
        """读今天已推送过的时点（落库，断电重载后不会把同一班重发一遍）。"""
        try:
            raw = self._storage.get_meta("estimate_push_done") or ""
            if not raw:
                return set()
            data = json.loads(raw)
            if data.get("date") == time.strftime("%Y-%m-%d"):
                return set(str(s) for s in (data.get("slots") or []))
        except Exception:
            pass
        return set()

    def _push_done_save(self, slots: set[str]) -> None:
        try:
            self._storage.set_meta("estimate_push_done", json.dumps(
                {"date": time.strftime("%Y-%m-%d"), "slots": sorted(slots)}, ensure_ascii=False))
        except Exception:
            pass

    async def _estimate_push_tick(self, now) -> None:
        """调度器每分钟调一次：命中配置时点（正常 25 分钟内补发）就推一次，当天同一时点只推一次。"""
        if not _plugin_cfg_value("estimate_push_enabled", self.cfg.get("estimate_push_enabled", True)):
            return
        today = time.strftime("%Y-%m-%d")
        if not _is_market_open(today):
            return
        done = self._push_done_load()
        cur_min = now.tm_hour * 60 + now.tm_min
        for slot in self._estimate_push_slots():
            if slot in done:
                continue
            try:
                h, m = map(int, slot.split(":"))
            except Exception:
                continue
            diff = cur_min - (h * 60 + m)
            # 0~25 分钟内补发：调度每 60 秒一轮，偶尔漂移也不会漏掉；错过太久则不发（避免夜里补发午休）
            if 0 <= diff <= 25:
                done.add(slot)
                self._push_done_save(done)
                await self._push_intraday_estimate(slot)
                break  # 一轮最多补一个时点，避免两个时点挤在一起连发
        try:
            self._maybe_reset_us_baseline(now)
        except Exception as e:
            logger.warning(f"[羊村基金涨跌榜] 美股基准重置失败: {e}")

    async def _estimate_catchup_on_boot(self) -> None:
        """开机/重载后补发：电脑关机错过的估值班次，在 3 小时内补一张（只补最近一班）。
        补的是「最近一个已过时点」，更早的时点直接标记为已过，避免开机后连发好几张。"""
        try:
            if not _plugin_cfg_value("estimate_push_enabled",
                                     self.cfg.get("estimate_push_enabled", True)):
                return
            today = time.strftime("%Y-%m-%d")
            now = time.localtime()
            if not _is_market_open(today):
                return
            if _game_mode_active():
                return
            done = self._push_done_load()
            cur_min = now.tm_hour * 60 + now.tm_min
            missed = []
            for slot in self._estimate_push_slots():
                if slot in done:
                    continue
                try:
                    h, m = map(int, slot.split(":"))
                except Exception:
                    continue
                diff = cur_min - (h * 60 + m)
                if diff >= 2:
                    missed.append((diff, slot))
            if not missed:
                return
            missed.sort()                       # 由近到远
            recent = missed[0]
            older = [s for _d, s in missed[1:]]
            # 太早的时点（>3 小时）不值得补，直接记为已过
            if older:
                done.update(older)
            if recent[0] > 180:
                if older or recent[1] not in done:
                    done.add(recent[1])
                    self._push_done_save(done)
                return
            done.add(recent[1])
            self._push_done_save(done)
            logger.info(f"[羊村基金涨跌榜] 开机补发估值：错过 {recent[1]}（已过 {recent[0]} 分钟）")
            await self._push_intraday_estimate(recent[1])
        except Exception as e:
            logger.error(f"[羊村基金涨跌榜] 开机补发估值失败: {e}")

    async def _run_daily_update(self) -> None:
        if _game_mode_active():
            logger.info("[羊村基金涨跌榜] 游戏模式开启，跳过本轮每日净值更新。")
            return
        total, waiting = await self._do_update()
        settle_notices = getattr(self, "_last_settle_notice", None) or []
        if total == 0 and not settle_notices:
            # 没有任何新净值（常见于晚间只在追 QDII 的滞后净值），静默跳过，不打扰群
            return
        today = time.strftime("%Y-%m-%d")
        # 一天只发一条「已更新」通知：净值是分批发布的，调度器每 40 分钟补一轮，
        # 每轮都发会把群刷屏（转换结算是一次性事件，照常随这条一起发）。
        # 用库里的 meta 记住当天已发过，插件重启/热重载也不会重复发。
        if not settle_notices:
            if getattr(self, "_notified_day", "") == today or \
                    self._storage.get_meta("notified_day") == today:
                logger.info(f"[羊村基金涨跌榜] {today} 已发过更新通知，本轮静默（更新 {total} 条）")
                self._notified_day = today
                return
        parts = [f"📈 基金净值已更新 {total} 条" + (f"（{waiting} 只待补）" if waiting else "")]
        parts.extend(settle_notices)
        pend = self._storage.pending_records()
        if pend:
            parts.append(f"⏳ {len(pend)} 条待确认未计入")
        parts.append("回复「涨跌榜」看明细。")
        ok_sent, msg_sent = await self._broadcast_text_async("\n".join(parts))
        logger.info(f"[羊村基金涨跌榜] 更新通知已发送（{msg_sent}，{len(parts)} 行）")
        self._notified_day = today
        try:
            self._storage.set_meta("notified_day", today)
        except Exception:
            pass
        # 净值一变，桌面组件立即同步（不用等 exe 自己的轮询）
        _notify_widget("每日更新")

    def _qdii_behind_today(self, today: str) -> bool:
        """是否还有已确认的 QDII 记录没拿到今天或更晚的净值。
        QDII 净值 T+1/T+2 滞后、常在晚间才发布，需要多追几轮，等它一发布就刷进去。"""
        for r in self._storage.all_records():
            if not r["confirmed"]:
                continue
            if "QDII" not in (r["fund_name"] or "").upper():
                continue
            if (r["last_update"] or "") < today:
                return True
        return False

    def _all_nav_done_today(self, today: str) -> bool:
        """判断当天是否所有已确认记录都已拿到当日净值（QDII 用最新净值，视为完成）。"""
        for r in self._storage.all_records():
            if not r["confirmed"]:
                continue
            name = r["fund_name"] or ""
            if "QDII" in name.upper():
                continue
            if (r["last_update"] or "") == today:
                continue
            return False
        return True

    async def _broadcast_text_async(self, text: str) -> tuple[bool, str]:
        # 广播目标：只发当前在用的那个群（动态解析，旧群注销后不再静默失败）
        # 同群多账号 umo 只发一次（第二个只做失败兜底），否则群里会看到两条一样的消息
        targets = self._group_targets(self._plugin_group_gid())
        if not targets:
            return False, "未找到群会话"
        ok = 0
        total = len(targets)
        if self._bot_api is not None:
            for gid, umos in targets:
                for umo in umos:
                    try:
                        ret = await self._bot_api.post_group_message(
                            group_openid=gid, content=text, msg_type=0)
                        mid = self._extract_mid(ret)
                        if mid:
                            asyncio.create_task(
                                self._recall_after(self._bot_api, gid, str(mid), RECALL_NOTICE_SEC))
                        ok += 1
                        break  # 该群已发出
                    except Exception as e:
                        logger.warning(f"[羊村基金涨跌榜] 广播发送失败 {umo}: {e}")
            return ok == total, f"已发送 {ok}/{total} 个群"
        cfg = _load_cmd_config()
        if not cfg:
            return False, "未找到 cmd_config.json"
        dash = cfg.get("dashboard") or {}
        secret = dash.get("jwt_secret")
        username = dash.get("username") or "astrbot"
        if not secret:
            return False, "未找到 jwt_secret"
        token = _make_jwt(secret, username)
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            for gid, umos in targets:
                for umo in umos:
                    sent = False
                    for attempt in range(2):
                        r = None
                        try:
                            r = await client.post(
                                f"http://127.0.0.1:6185/api/v1/im/messages",
                                headers={"Authorization": f"Bearer {token}"},
                                json={"message": text, "umo": umo})
                        except Exception:
                            r = None
                        if r is not None and r.status_code < 300:
                            # 先认定「已发出」再解析 mid：解析失败绝不能触发重发（会重复刷屏）
                            sent = True
                            ok += 1
                            try:
                                mid = self._extract_http_mid(r)
                                if mid and self._bot_api:
                                    asyncio.create_task(
                                        self._recall_after(self._bot_api, gid, str(mid),
                                                           RECALL_NOTICE_SEC))
                            except Exception:
                                pass
                            break
                        await asyncio.sleep(0.6)
                    if sent:
                        break  # 该群已发出
                await asyncio.sleep(0.4)
        return ok == total, f"已发送 {ok}/{total} 个群"

    @staticmethod
    def _extract_http_mid(r) -> str | None:
        """从 im/messages 的 HTTP 响应中尽量提取消息 id（结构可能随版本变化，尽力而为）。"""
        try:
            data = r.json()
        except Exception:
            return None
        if isinstance(data, dict):
            for k in ("message_id", "msg_id", "id", "messageId"):
                v = data.get(k)
                if v:
                    return str(v)
            inner = data.get("data")
            if isinstance(inner, dict):
                for k in ("message_id", "msg_id", "id", "messageId"):
                    v = inner.get(k)
                    if v:
                        return str(v)
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            for k in ("message_id", "msg_id", "id", "messageId"):
                v = data[0].get(k)
                if v:
                    return str(v)
        return None
