# -*- coding: utf-8 -*-
"""群趣事漫画图渲染 v0.2 —— 聊天截图风 + 真实头像 + 热度/参与人数。

story:
{
  "title": "羊村今日趣事", "group": "羊村", "time": "10-28 18:40",
  "window_minutes": 120, "total_messages": 86, "total_people": 12,
  "events": [{
    "topic": "...", "desc": "...", "heat": 18, "participants": 6,
    "time": "18:32–18:37",
    "dialogues": [{"name": "阿伟", "text": "..."}]
  }]
}
实际头像由 main 下载后以 {昵称: 本地路径} 传入 avatars。
"""
from __future__ import annotations

import hashlib
import os
import warnings

from PIL import Image, ImageDraw, ImageFont, ImageOps

FONT_DIR = r"C:\Windows\Fonts"
FONT_TITLE = os.path.join(FONT_DIR, "msyhbd.ttc")
FONT_BODY = os.path.join(FONT_DIR, "msyh.ttc")
FONT_FALLBACK = os.path.join(FONT_DIR, "simhei.ttf")

W = 820
PAD = 30
CARD_W = W - PAD * 2
CARD_PAD = 24
HEADER_H = 176
FOOTER_H = 88

BG = "#F2F4F8"
CARD = "#FFFFFF"
SHADOW = "#E1E6EE"
TEXT = "#20242B"
SUB = "#7D8490"
BORDER = "#E4E8EF"
BOT_BUBBLE = "#DDF0FF"
BOT_BORDER = "#AFD8F4"
OTHER_BUBBLE = "#F7F8FA"
OTHER_BORDER = "#E2E6EC"

HEADER_TOP = (255, 112, 67)
HEADER_BOT = (255, 170, 64)
ACCENTS = ["#FF7A45", "#4A90D9", "#3CBF8B", "#8E6FD8"]
AVATAR_COLORS = ["#4A90D9", "#E0443C", "#3CBF6B", "#F5A623", "#8E6FD8", "#2BB3B3", "#E86CA9", "#B8860B"]

FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
AVATAR_CACHE: dict[str, Image.Image] = {}


def _font(path: str, size: int):
    key = (path, size)
    if key in FONT_CACHE:
        return FONT_CACHE[key]
    use = path if os.path.exists(path) else FONT_FALLBACK
    try:
        font = ImageFont.truetype(use, size)
    except OSError:
        font = ImageFont.load_default()
    FONT_CACHE[key] = font
    return font


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    text = str(text or "")
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _avatar_color(name: str) -> str:
    h = hashlib.md5((name or "?").encode("utf-8")).hexdigest()
    return AVATAR_COLORS[int(h, 16) % len(AVATAR_COLORS)]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    text = str(text or "").replace("\r", "")
    if not text:
        return [""]
    out: list[str] = []
    for raw_line in text.split("\n"):
        cur = ""
        for ch in raw_line:
            if draw.textlength(cur + ch, font=font) <= max_w:
                cur += ch
            else:
                if cur:
                    out.append(cur)
                cur = ch
        out.append(cur)
    return out or [""]


def _rounded_shadow(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    radius: int = 20,
    offset: int = 5,
) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle((x0 + offset, y0 + offset, x1 + offset, y1 + offset), radius, fill=SHADOW)
    draw.rounded_rectangle(box, radius, fill=CARD)


def _load_avatar(path: str | None, size: int) -> Image.Image | None:
    if not path or not os.path.isfile(path):
        return None
    key = f"{path}:{size}:{os.path.getmtime(path)}"
    if key in AVATAR_CACHE:
        return AVATAR_CACHE[key]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            im = Image.open(path)
            im.load()
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, "#FFFFFF")
                bg.paste(rgba, mask=rgba.getchannel("A"))
                im = bg
            else:
                im = im.convert("RGB")
        im = ImageOps.fit(im, (size, size), Image.Resampling.LANCZOS, centering=(0.5, 0.4))
        AVATAR_CACHE[key] = im
        return im
    except Exception:  # noqa: BLE001
        return None


def _paste_avatar(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    name: str,
    path: str | None,
    x: int,
    y: int,
    size: int = 46,
) -> None:
    im = _load_avatar(path, size * 3)
    if im is not None:
        im = im.resize((size, size), Image.Resampling.LANCZOS)
        mask = Image.new("L", (size * 3, size * 3), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size * 3 - 1, size * 3 - 1), fill=255)
        mask = mask.resize((size, size), Image.Resampling.LANCZOS)
        img.paste(im, (x, y), mask)
        draw.ellipse((x, y, x + size - 1, y + size - 1), outline="#FFFFFF", width=2)
        return
    color = _avatar_color(name)
    draw.ellipse((x, y, x + size - 1, y + size - 1), fill=color)
    ch = (name or "?")[:1]
    f = _font(FONT_TITLE, int(size * 0.52))
    tw = draw.textlength(ch, font=f)
    draw.text((x + (size - tw) / 2, y + size * 0.18), ch, font=f, fill="#FFFFFF")


def _bubble(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    fill: str,
    border: str,
    tail_left: bool,
) -> None:
    draw.rounded_rectangle((x, y, x + w, y + h), radius=16, fill=fill, outline=border, width=1)
    ty = y + 17
    if tail_left:
        draw.polygon([(x + 1, ty), (x - 8, ty + 9), (x + 1, ty + 18)], fill=fill, outline=border)
        draw.line((x + 1, ty + 1, x + 1, ty + 17), fill=fill, width=2)
    else:
        draw.polygon([(x + w - 1, ty), (x + w + 8, ty + 9), (x + w - 1, ty + 18)], fill=fill, outline=border)
        draw.line((x + w - 1, ty + 1, x + w - 1, ty + 17), fill=fill, width=2)


def _layout_event(draw: ImageDraw.ImageDraw, event: dict, idx: int) -> dict:
    topic_font = _font(FONT_TITLE, 28)
    desc_font = _font(FONT_BODY, 21)
    name_font = _font(FONT_BODY, 19)
    body_font = _font(FONT_BODY, 22)
    accent = ACCENTS[idx % len(ACCENTS)]

    inner_w = CARD_W - CARD_PAD * 2
    topic = str(event.get("topic") or "").strip()
    topic_max = max(120, inner_w - 50 - 160)
    topic_lines = _wrap(draw, topic, topic_font, topic_max)
    desc_lines = _wrap(draw, str(event.get("desc") or ""), desc_font, inner_w)
    head_h = max(42, len(topic_lines) * 34) + len(desc_lines) * 28 + 12

    rows: list[dict] = []
    for d in (event.get("dialogues") or [])[:4]:
        name = str(d.get("name") or "")
        text = str(d.get("text") or "")
        is_bot = "乐乐" in name
        max_bubble = int(inner_w * 0.64)
        lines = _wrap(draw, text, body_font, max_bubble - 28)
        tw = max((draw.textlength(line, font=body_font) for line in lines), default=0)
        bw = max(110, min(max_bubble, int(tw) + 28))
        bh = len(lines) * 30 + 22
        row_h = 28 + bh + 12
        rows.append(
            {
                "name": name,
                "lines": lines,
                "bw": bw,
                "bh": bh,
                "row_h": row_h,
                "is_bot": is_bot,
                "font": name_font,
                "body_font": body_font,
            }
        )
    return {
        "accent": accent,
        "topic": topic,
        "topic_lines": topic_lines,
        "topic_font": topic_font,
        "topic_max": topic_max,
        "desc_lines": desc_lines,
        "desc_font": desc_font,
        "head_h": head_h,
        "rows": rows,
        "height": 22 + head_h + sum(r["row_h"] for r in rows) + 14,
    }


def render_summary(story: dict, out_path: str, avatars: dict[str, str] | None = None) -> str:
    avatars = avatars or {}
    events = [e for e in (story.get("events") or []) if isinstance(e, dict)][:3]
    if not events:
        events = [
            {
                "topic": "今天没什么好玩的",
                "desc": "大家都很安静~",
                "dialogues": [{"name": "乐乐", "text": "最近群里太安静了，啥都没发生。"}],
                "heat": 0,
                "participants": 0,
            }
        ]

    title = str(story.get("title") or "群趣事")[:30]
    group = str(story.get("group") or "")
    time_s = str(story.get("time") or "")
    window = int(story.get("window_minutes") or 0)
    total_msgs = int(story.get("total_messages") or 0)
    total_people = int(story.get("total_people") or 0)

    temp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    layouts = [_layout_event(temp, ev, i) for i, ev in enumerate(events)]
    H = HEADER_H + PAD + sum(lay["height"] + 22 for lay in layouts) + FOOTER_H
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # header gradient
    for y in range(HEADER_H):
        t = y / max(1, HEADER_H - 1)
        col = tuple(int(HEADER_TOP[i] + (HEADER_BOT[i] - HEADER_TOP[i]) * t) for i in range(3))
        draw.line((0, y, W, y), fill=col)
    # decorative circles
    draw.ellipse((W - 190, -80, W + 80, 190), fill="#FFC06A")
    draw.ellipse((-90, 90, 130, 310), fill="#FF8A50")
    draw.ellipse((W - 120, 120, W + 40, 280), fill="#FFD08A")

    badge_font = _font(FONT_TITLE, 17)
    draw.rounded_rectangle((PAD, 22, PAD + 104, 52), radius=15, fill="#FFFFFF")
    draw.text((PAD + 14, 27), "群趣事", font=badge_font, fill="#E8612A")

    title_font = _font(FONT_TITLE, 40)
    title = _ellipsize(draw, title, title_font, W - PAD * 2)
    tw = draw.textlength(title, font=title_font)
    draw.text(((W - tw) / 2, 62), title, font=title_font, fill="#FFFFFF")
    sub_font = _font(FONT_BODY, 21)
    sub = " · ".join(x for x in (group, time_s) if x)
    if sub:
        sub = _ellipsize(draw, sub, sub_font, W - PAD * 2)
        sw = draw.textlength(sub, font=sub_font)
        draw.text(((W - sw) / 2, 114), sub, font=sub_font, fill="#FFF4E8")

    stats = []
    if window:
        stats.append(f"最近 {window} 分钟")
    if total_msgs:
        stats.append(f"{total_msgs} 条消息")
    if total_people:
        stats.append(f"{total_people} 位群友")
    if stats:
        text = "  ·  ".join(stats)
        sw = draw.textlength(text, font=sub_font)
        draw.rounded_rectangle(((W - sw) / 2 - 14, 140, (W + sw) / 2 + 14, 166), radius=13, fill="#FFFFFF")
        draw.text(((W - sw) / 2, 143), text, font=sub_font, fill="#C24E1E")

    # cards
    y = HEADER_H + PAD
    for i, (ev, lay) in enumerate(zip(events, layouts)):
        x0 = PAD
        x1 = x0 + CARD_W
        h = lay["height"]
        _rounded_shadow(draw, (x0, y, x1, y + h), 20, 5)
        draw.rounded_rectangle((x0, y + 16, x0 + 6, y + h - 16), radius=3, fill=lay["accent"])

        cx = x0 + CARD_PAD
        cw = CARD_W - CARD_PAD * 2
        cy = y + 20
        # number
        draw.rounded_rectangle((cx, cy + 2, cx + 36, cy + 38), radius=10, fill=lay["accent"])
        num = str(i + 1)
        nf = _font(FONT_TITLE, 22)
        nw = draw.textlength(num, font=nf)
        draw.text((cx + (36 - nw) / 2, cy + 7), num, font=nf, fill="#FFFFFF")

        # topic
        tx = cx + 50
        topic_max = lay["topic_max"]
        topic_lines = _wrap(draw, lay["topic"], lay["topic_font"], topic_max)
        ty = cy + 1
        for line in topic_lines[:2]:
            draw.text((tx, ty), line, font=lay["topic_font"], fill=TEXT)
            ty += 34

        # stats pill
        heat = int(ev.get("heat") or 0)
        people = int(ev.get("participants") or 0)
        pill = f"热度 {heat} · {people} 人"
        pf = _font(FONT_BODY, 18)
        pw = draw.textlength(pill, font=pf) + 22
        px1 = x1 - CARD_PAD
        px0 = px1 - pw
        draw.rounded_rectangle((px0, cy + 4, px1, cy + 34), radius=15, fill="#FFF0E7")
        draw.text((px0 + 11, cy + 8), pill, font=pf, fill="#D75B22")

        cy += max(42, len(topic_lines) * 34) + 2
        for line in lay["desc_lines"]:
            draw.text((cx, cy), line, font=lay["desc_font"], fill=SUB)
            cy += 28
        cy += 8

        # dialogues
        for row in lay["rows"]:
            name = row["name"]
            is_bot = row["is_bot"]
            bw = row["bw"]
            bh = row["bh"]
            av = 46
            if is_bot:
                av_x = x0 + CARD_W - CARD_PAD - av
                bx = av_x - 14 - bw
                nx = av_x - 10
            else:
                av_x = x0 + CARD_PAD
                bx = av_x + av + 14
                nx = bx
            # name
            name_font = row["font"]
            name_w = draw.textlength(name, font=name_font)
            if is_bot:
                draw.text((nx - name_w, cy), name, font=name_font, fill="#3C7FB0")
            else:
                draw.text((nx, cy), name, font=name_font, fill="#6D7480")

            # avatar + bubble
            _paste_avatar(img, draw, name, _resolve_avatar(name, avatars), av_x, cy + 2, av)
            by = cy + 26
            fill, border = (BOT_BUBBLE, BOT_BORDER) if is_bot else (OTHER_BUBBLE, OTHER_BORDER)
            _bubble(draw, bx, by, bw, bh, fill, border, tail_left=not is_bot)
            ty2 = by + 11
            for line in row["lines"]:
                draw.text((bx + 14, ty2), line, font=row["body_font"], fill=TEXT)
                ty2 += 30
            cy += row["row_h"]

        # bottom padding already included
        y += h + 22

    # footer
    y += 4
    foot_font = _font(FONT_BODY, 19)
    foot = "乐乐 · 群趣事  |  头像来自群成员资料，对话为原话摘录"
    fw = draw.textlength(foot, font=foot_font)
    draw.text(((W - fw) / 2, y), foot, font=foot_font, fill=SUB)
    y += 34
    mark_font = _font(FONT_BODY, 16)
    mark = "按话题连贯性选段，原话按编号回查，不跨片段编造"
    mw = draw.textlength(mark, font=mark_font)
    draw.text(((W - mw) / 2, y), mark, font=mark_font, fill="#A6ACB6")

    img.crop((0, 0, W, min(H, y + 28))).save(out_path, "PNG")
    return out_path


def _resolve_avatar(name: str, avatars: dict[str, str]) -> str | None:
    if not name:
        return None
    if name in avatars:
        return avatars[name]
    for real, path in avatars.items():
        if name in real or real in name:
            return path
    return None
