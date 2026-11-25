# -*- coding: utf-8 -*-
"""astrbot_plugin_tg_watch —— Telegram 指定群增量播报。

两种用法并存：
1. 定时：每 N 分钟（默认 5）自动巡检一次，有新消息就把结果渲染成一张卡片图发到群；
2. 手动：群里发 /tg，立即查一次并回一张同样的卡片图。

只播报「新增几条 / 哪段时间 / 谁说得多 / 是否命中特别关注」，不回传任何消息正文。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .render import render_card
from .tg_core import TgError, TgWatchCore, TgWorker

PLUGIN_NAME = "astrbot_plugin_tg_watch"


@register(
    PLUGIN_NAME,
    "乐乐",
    "TG 群增量播报：每 5 分钟自动把新增消息渲染成卡片图发到群，lgd 发言显示「LGD的奇妙冒险」",
    "1.1.0",
)
class TgWatchPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._core: TgWatchCore | None = None
        self._worker: TgWorker | None = None
        self._watch_task: asyncio.Task | None = None
        self._warned_no_target = False
        logger.info("[tg_watch] 数据目录：%s", self._data_dir)

    # ------------------------------------------------------------------ 工具
    def _ensure(self) -> TgWatchCore:
        """全局只保留一个 core / worker，连接和群实体缓存才不会每次重来。"""
        if self._core is None:
            self._core = TgWatchCore(
                self.config,
                self._data_dir,
                log=lambda msg: logger.info("[tg_watch] %s", msg),
            )
            self._worker = TgWorker(self._core)
        return self._core

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            raw = self.config.get(key)
            return int(raw) if raw not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        raw = self.config.get(key)
        if raw is None or raw == "":
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")

    def _timeout(self) -> float:
        return max(20, min(300, self._cfg_int("timeout_seconds", 60)))

    def _target_umo(self) -> str:
        """固定配置优先；没配就用群里发过 /tg 记下来的那个会话。"""
        fixed = str(self.config.get("notify_umo") or "").strip()
        if fixed:
            return fixed
        core = self._ensure()
        return str(core.load_notify().get("umo") or "").strip()

    async def _check_once(self) -> dict:
        """跑一次增量检查：Telethon 在常驻线程的事件循环里执行，不阻塞主循环。"""
        self._ensure()
        timeout = self._timeout()
        return await asyncio.to_thread(self._worker.call, timeout)

    async def _send_card(self, umo: str, result: dict) -> bool:
        """渲染一张卡片图并推送；失败时退回纯文本，保证消息不丢。"""
        try:
            path = await asyncio.to_thread(render_card, result, self.config, self._data_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tg_watch] 渲染卡片失败，改发文字：%s", exc)
            text = self._ensure().format_result(result)
            return bool(await self.context.send_message(umo, MessageChain().message(text)))
        try:
            return bool(
                await self.context.send_message(umo, MessageChain().file_image(str(path)))
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tg_watch] 发图失败，改发文字：%s", exc)
            text = self._ensure().format_result(result)
            return bool(await self.context.send_message(umo, MessageChain().message(text)))

    # -------------------------------------------------------------- 生命周期
    async def initialize(self) -> None:
        self._ensure()
        if not self._cfg_bool("auto_watch", True):
            logger.info("[tg_watch] 自动监听已关闭（auto_watch=false）")
            return
        interval = max(1, self._cfg_int("auto_interval_minutes", 5))
        self._watch_task = asyncio.create_task(self._watch_loop(interval))
        logger.info("[tg_watch] 自动监听已启动：每 %s 分钟一次", interval)

    async def terminate(self) -> None:
        task, self._watch_task = self._watch_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        worker, self._worker = self._worker, None
        if worker is not None:
            try:
                await asyncio.to_thread(worker.stop)
            except Exception:  # noqa: BLE001
                pass
        self._core = None

    async def _watch_loop(self, minutes: int) -> None:
        interval = minutes * 60
        # 首次立即跑一次：没有游标时只建基线（不打扰群），之后每轮才有增量
        while True:
            try:
                await self._auto_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[tg_watch] 定时巡检出错：%s", exc)
            await asyncio.sleep(interval)

    async def _auto_tick(self) -> None:
        if self._lock.locked():  # 手动查询正在跑，这轮让给它
            return
        async with self._lock:
            result = await self._check_once()

        if result.get("first_run"):
            logger.info(
                "[tg_watch] 建立基线 #%s，之后每轮播报新增消息",
                result.get("latest_id"),
            )
            return
        total = int(result.get("total") or 0)
        if total <= 0:
            return

        umo = self._target_umo()
        if not umo:
            if not self._warned_no_target:
                self._warned_no_target = True
                logger.warning(
                    "[tg_watch] 还没有推送目标：在要接收播报的群里发一次 /tg，之后自动推送就锁定那个群"
                )
            return

        ok = await self._send_card(umo, result)
        logger.info(
            "[tg_watch] 自动播报：新增 %s 条，命中 %s 条，推送%s，耗时 %ss",
            total,
            result.get("hit_count", 0),
            "成功" if ok else "失败",
            result.get("elapsed"),
        )

    # ------------------------------------------------------------------ 指令
    @filter.command("tg", alias={"tg更新", "tg查询"})
    async def tg_check(self, event: AstrMessageEvent):
        """立即查一次并回一张卡片图。"""
        event.stop_event()

        # 群里发过一次 /tg，就把这个会话记成自动播报的目标
        umo_now = str(getattr(event, "unified_msg_origin", "") or "")
        if ":GroupMessage:" in umo_now:
            self._ensure().save_notify(umo_now)

        cooldown = max(0, self._cfg_int("cooldown_seconds", 10))
        remain = cooldown - (time.time() - self._last_call)
        if remain > 0:
            yield event.plain_result(f"⏳ 刚查过，{int(remain) + 1} 秒后再试")
            return
        if self._lock.locked():
            yield event.plain_result("⏳ 上一轮还在跑，稍等一下")
            return

        async with self._lock:
            self._last_call = time.time()
            try:
                result = await self._check_once()
            except TgError as exc:
                yield event.plain_result(f"❌ {exc}")
                return
            except TimeoutError:
                logger.warning("[tg_watch] 查询超时")
                yield event.plain_result("❌ Telegram 查询超时，网络或代理不通，等下再试")
                return
            except Exception as exc:  # noqa: BLE001
                logger.error("[tg_watch] 查询失败：%s", exc, exc_info=True)
                yield event.plain_result(f"❌ Telegram 查询失败：{exc}")
                return

        logger.info(
            "[tg_watch] 手动查询完成：%s 新增 %s 条，命中 %s 条，耗时 %ss",
            result.get("chat_title"),
            result.get("total", "基线"),
            result.get("hit_count", 0),
            result.get("elapsed"),
        )

        # 没新增就不出图，只回一句话，省得群里刷图
        if not result.get("first_run") and int(result.get("total") or 0) <= 0:
            title = result.get("chat_title") or "Telegram 群"
            checked = result.get("checked_at")
            tail = f"（上次检查 {checked}）" if checked else ""
            yield event.plain_result(f"📡 {title}：暂无新消息{tail}")
            return

        try:
            path = await asyncio.to_thread(render_card, result, self.config, self._data_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tg_watch] 渲染卡片失败，改发文字：%s", exc)
            yield event.plain_result(self._ensure().format_result(result))
            return
        yield event.image_result(str(path))

    @filter.command("tg状态", alias={"tg状态查询"})
    async def tg_status(self, event: AstrMessageEvent):
        """排查用：游标停在哪、自动监听开没开、推送到哪。"""
        event.stop_event()
        core = self._ensure()
        state = core.load_state()
        interval = max(1, self._cfg_int("auto_interval_minutes", 5))
        auto = self._cfg_bool("auto_watch", True)
        lines = [
            f"📡 监看：{state.get('chat_title') or '未知'}（id={state.get('chat_id') or '未建基线'}）",
            f"游标：#{state.get('last_id') or '未建基线'}",
            f"上次检查：{state.get('checked_at') or '未知'}",
            f"自动监听：{'开，每 ' + str(interval) + ' 分钟' if auto else '关'}",
            f"推送目标：{self._target_umo() or '未设置（在目标群发一次 /tg 即可）'}",
            f"连接状态：{'已连着（常驻复用）' if core.is_connected() else '暂未连接，用时自动连'}",
            f"关键词：{core.keyword} → {core.special_text}",
        ]
        yield event.plain_result("\n".join(lines))
