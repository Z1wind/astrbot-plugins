# -*- coding: utf-8 -*-
"""把一次增量检查结果渲染成一张简洁卡片图（纯本地 Pillow，不走网络/大模型）。

设计目标：一条消息一张图、留白宽松、信息只有「群名 / 新增几条 / 时间段 / 谁在说 / 特别标注」。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(r"C:\Windows\Fonts")
_BOLD_FONTS = ("msyhbd.ttc", "msyh.ttc", "simhei.ttf")
_BODY_FONTS = ("msyh.ttc", "simhei.ttf")

W = 700
PAD = 40
RADIUS = 24
BG = (236, 239, 244)
CARD = (255, 255, 255)
C_TITLE = (24, 28, 35)
C_MUTED = (140, 147, 157)
C_NUM = (20, 24, 30)
C_ACCENT = (226, 84, 38)
C_PILL_BG = (255, 241, 234)
C_RULE = (233, 236, 241)

_FONTS: "dict[tuple[str, int], ImageFont.FreeTypeFont]" = {}


def _font(bold: bool, size: int):
    """按 (粗/常规, 字号) 缓存字体对象，避免每张图都重新解析 ttc。"""
    key = ("b" if bold else "r", size)
    cached = _FONTS.get(key)
    if cached is not None:
        return cached
    names = _BOLD_FONTS if bold else _BODY_FONTS
    font = None
    for name in names:
        try:
            font = ImageFont.truetype(str(FONT_DIR / name), size)
            break
        except Exception:  # noqa: BLE001
            font = None
    if font is None:
        font = ImageFont.load_default()
    _FONTS[key] = font
    return font


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    suffix = "…"
    while text and draw.textlength(text + suffix, font=font) > max_w:
        text = text[:-1]
    return (text + suffix) if text else suffix


def _senders_line(counts: dict, limit: int = 3) -> str:
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if not items:
        return ""
    head = " · ".join(f"{name} {num}" for name, num in items[:limit])
    if len(items) > limit:
        head += f" 等 {len(items)} 人"
    return head


def _write_card(result: dict, data_dir: Path) -> Path:
    title = str(result.get("chat_title") or "Telegram 群")
    first_run = bool(result.get("first_run"))
    total = int(result.get("total") or 0)
    more = bool(result.get("more"))
    span = str(result.get("span") or "")
    counts = dict(result.get("senders") or {})
    hits = list(result.get("hits") or [])
    fwd = int(result.get("forwards") or 0)
    special = str(result.get("special") or "LGD的奇妙冒险")
    show_senders = bool(result.get("show_senders", True))
    clock = datetime.now().strftime("%H:%M")

    f_title = _font(True, 29)
    f_clock = _font(False, 17)
    f_num = _font(True, 54)
    f_unit = _font(False, 22)
    f_body = _font(False, 19)
    f_pill = _font(True, 21)

    inner_w = W - PAD * 2

    # ---------- 先量一遍行宽，决定高度 ----------
    probe = Image.new("RGB", (W, 10), CARD)
    pd = ImageDraw.Draw(probe)
    title_txt = _ellipsize(pd, title, f_title, inner_w - 80)

    if first_run:
        sub_txt = f"基线 #{result.get('latest_id')}，之后的新消息自动播报"
    elif total > 0:
        sub_txt = span or "时间未知"
    else:
        sub_txt = "暂无新消息" + (f" · 上次检查 {result.get('checked_at')}" if result.get("checked_at") else "")

    senders_txt = ""
    if show_senders and total > 0 and counts:
        senders_txt = _ellipsize(pd, "发言人 " + _senders_line(counts), f_body, inner_w)

    fwd_txt = f"含转发 {fwd} 条" if (fwd > 0 and not first_run) else ""

    pill_w = int(pd.textlength(special, font=f_pill)) + 56
    pill_h = 46

    height = PAD + 36 + 22 + 66 + 10 + 26
    if senders_txt:
        height += 10 + 28
    if fwd_txt:
        height += 10 + 28
    if hits:
        height += 16 + pill_h
    height += PAD

    img = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, W - 1, height - 1], radius=RADIUS, fill=CARD)

    # ---------- 标题行 ----------
    y = PAD
    draw.text((PAD, y), title_txt, font=f_title, fill=C_TITLE)
    clock_w = pd.textlength(clock, font=f_clock)
    draw.text((W - PAD - clock_w, y + 8), clock, font=f_clock, fill=C_MUTED)
    y += 36

    draw.line([(PAD, y + 12), (W - PAD, y + 12)], fill=C_RULE, width=1)
    y += 22

    # ---------- 主数字 ----------
    if first_run:
        draw.text((PAD, y), "已接入监看", font=_font(True, 38), fill=C_TITLE)
        y += 66
    else:
        num_txt = f"{total}{'+' if more else ''}"
        draw.text((PAD, y - 4), num_txt, font=f_num, fill=C_NUM)
        num_w = pd.textlength(num_txt, font=f_num)
        # 让「条新消息」和小字与数字落在同一条基线上，看着才不飘
        base_y = (y - 4) + f_num.getmetrics()[0]
        unit_y = base_y - f_unit.getmetrics()[0]
        draw.text((PAD + num_w + 10, unit_y), "条新消息", font=f_unit, fill=C_MUTED)
        y += 66

    # ---------- 时间段 ----------
    sub = _ellipsize(pd, sub_txt, f_body, inner_w)
    draw.text((PAD, y), sub, font=f_body, fill=C_MUTED)
    y += 26

    # ---------- 发言人 ----------
    if senders_txt:
        y += 10
        draw.text((PAD, y), senders_txt, font=f_body, fill=(96, 104, 116))
        y += 28

    # ---------- 转发提示 ----------
    if fwd_txt:
        y += 10
        draw.text((PAD, y), fwd_txt, font=f_body, fill=(70, 110, 168))
        y += 28

    # ---------- 特别标注 ----------
    if hits:
        y += 16
        draw.rounded_rectangle(
            [PAD, y, PAD + pill_w, y + pill_h], radius=pill_h // 2, fill=C_PILL_BG
        )
        pill_y = y + (pill_h - 21) / 2 - 3
        draw.text((PAD + 28, pill_y), special, font=f_pill, fill=C_ACCENT)
        # 左侧小圆点，替代 emoji，保证任何字体下都能画出来
        cx, cy = PAD + 16, y + pill_h / 2
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=C_ACCENT)

    cards = Path(data_dir) / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    out = cards / f"tg_{int(time.time() * 1000)}.png"
    img.save(out, "PNG", optimize=True)
    return out


def render_card(result: dict, config: dict, data_dir) -> Path:
    """渲染卡片并清理过期文件（只留最近 10 张，别把磁盘越堆越大）。"""
    data_dir = Path(data_dir)
    out = _write_card(result, data_dir)
    cards = data_dir / "cards"
    try:
        files = sorted(cards.glob("tg_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[10:]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
    return out
