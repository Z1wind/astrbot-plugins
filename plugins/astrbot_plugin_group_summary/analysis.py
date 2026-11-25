# -*- coding: utf-8 -*-
"""群趣事候选分析：按消息热度 + 参与人数找“最热闹的几分钟”。

纯标准库，方便单测；不依赖 AstrBot / Pillow。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


@dataclass
class Hotspot:
    index: int
    start: float
    end: float
    messages: list[dict] = field(default_factory=list)
    heat: int = 0
    participants: int = 0
    score: int = 0

    @property
    def names(self) -> list[str]:
        seen: list[str] = []
        for m in self.messages:
            name = str(m.get("name") or "").strip()
            if name and name not in seen:
                seen.append(name)
        return seen

    @property
    def time_range(self) -> str:
        return (
            f"{time.strftime('%H:%M', time.localtime(self.start))}"
            f"–{time.strftime('%H:%M', time.localtime(self.end))}"
        )

    def prompt_block(self, max_messages: int = 20) -> str:
        head = (
            f"【候选 {self.index}】{self.time_range}｜"
            f"消息 {self.heat} 条｜参与 {self.participants} 人｜综合分 {self.score}"
        )
        lines = [head]
        for m in self.messages[:max_messages]:
            text = _display_text(m.get("text"))
            if not text:
                continue
            lines.append(f"{m.get('name') or '群友'}: {text[:120]}")
        return "\n".join(lines)


_NOISE_PATTERNS = (
    "<?xml",
    "serviceID=",
    "jump_url:",
)
_MEDIA_MARK_RE = re.compile(
    r"\[(?:图片|视频|动画表情|表情|文件|语音|链接|卡片)\]"
)


def _display_text(text: object) -> str:
    """去掉媒体占位符，图里只留真正说出口的话。"""
    clean = _MEDIA_MARK_RE.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", clean).strip()


def _is_noise_text(text: object) -> bool:
    """纯媒体/卡片/XML 不算一句可用的话，别让它们占对话位。"""
    raw = str(text or "").strip()
    if not raw:
        return True
    if any(p in raw for p in _NOISE_PATTERNS):
        return True
    stripped = re.sub(r"\[[^\]]{1,10}\]", "", raw).strip()
    return not stripped


def _fmt_hhmm(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(float(ts)))


def build_transcript(
    messages: list[dict], max_messages: int = 260
) -> tuple[list[dict], str]:
    """给 LLM 的全量带编号 transcript。

    返回 (source, text)。source 的第 i 条对应 m{i+1:03d}，normalize 时按编号取原文，
    这样 LLM 只负责“选哪几条”，不负责复述，避免改写/编造原话。
    """
    items = [
        dict(m)
        for m in messages
        if m.get("ts") and str(m.get("text") or "").strip()
    ]
    items.sort(key=lambda m: float(m["ts"]))
    limit = max(1, min(500, int(max_messages or 260)))
    source = items[-limit:]
    lines: list[str] = []
    for i, m in enumerate(source, 1):
        text = _display_text(m.get("text"))[:110]
        lines.append(
            f"m{i:03d} {_fmt_hhmm(float(m['ts']))} "
            f"{m.get('name') or '群友'}: {text or '[媒体]'}"
        )
    return source, "\n".join(lines)


_QUOTE_RE = re.compile(r"[“\"「『]([^”\"」』]{2,40})[”\"」』]")


def _desc_is_grounded(
    desc: str, dialogues: list[dict], source_names: set[str]
) -> bool:
    """desc 里引用的原话/人物必须真的出现在选中的对话里。

    模型很容易在 desc 里补一句“某某回……”，但 ids 根本没选那条消息。
    这里先挡两类最常见的情况：引号里的话不在选中原文、提到某个群友却没选他的话。
    """
    if not desc:
        return False
    joined = " ".join(str(d.get("text") or "") for d in dialogues)
    for quoted in _QUOTE_RE.findall(desc):
        phrase = re.sub(r"\s+", "", quoted)
        if phrase and phrase not in re.sub(r"\s+", "", joined):
            return False
    dialogue_names = {str(d.get("name") or "") for d in dialogues}
    for name in source_names:
        if len(name) >= 2 and name in desc and name not in dialogue_names:
            return False
    return True


def _factual_desc(dialogues: list[dict]) -> str:
    """模型 desc 不靠谱时的兜底：只拿选中原话拼，不再二次发挥。"""
    parts = [
        f"{str(d.get('name') or '群友')}：“{str(d.get('text') or '')[:20]}”"
        for d in dialogues[:3]
    ]
    return "；".join(parts)[:80]


def _parse_message_id(value: object) -> int | None:
    if isinstance(value, int):
        return value if value > 0 else None
    match = re.fullmatch(r"m?(\d{1,3})", str(value or "").strip().lower())
    if not match:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def normalize_selection(
    story: dict,
    source: list[dict],
    group_name: str = "本群",
    *,
    max_events: int = 3,
    max_span_messages: int = 60,
    max_span_seconds: float = 1800.0,
    heat_window_seconds: float = 90.0,
) -> dict | None:
    """校验 LLM 选中的消息编号，并用源消息原文重建事件。

    LLM 只输出 ids（m001…），这里负责：
    - 编号必须存在、不能跨太远（最多 60 条 / 30 分钟）；
    - 过滤纯媒体/XML；
    - 对话原文从 source 取，不接受模型改写；
    - 热度/参与人数按选中消息前后 90 秒的局部发言算，不拿中间夹着的无关消息充数。
    """
    if not isinstance(story, dict) or not source:
        return None
    raw_events = story.get("events")
    if not isinstance(raw_events, list):
        return None

    used: set[int] = set()
    source_names = {
        str(m.get("name") or "").strip()
        for m in source
        if str(m.get("name") or "").strip()
    }
    events: list[dict] = []
    for raw in raw_events[: max(1, int(max_events))]:
        if not isinstance(raw, dict):
            continue
        ids: list[int] = []
        for value in raw.get("ids") or []:
            number = _parse_message_id(value)
            if number is None or number > len(source) or number in ids:
                continue
            ids.append(number)
        ids = sorted(i for i in ids if i not in used)
        if len(ids) < 3:
            continue
        first, last = ids[0], ids[-1]
        if last - first + 1 > max_span_messages:
            continue
        span = source[first - 1 : last]
        if not span:
            continue
        start_ts = float(source[first - 1]["ts"])
        end_ts = float(source[last - 1]["ts"])
        if end_ts - start_ts > max_span_seconds:
            continue

        selected = [source[i - 1] for i in ids]
        selected = [m for m in selected if not _is_noise_text(m.get("text"))]
        if len(selected) < 3:
            continue

        names: list[str] = []
        dialogues: list[dict] = []
        for m in selected:
            name = str(m.get("name") or "群友").strip() or "群友"
            text = _display_text(m.get("text"))[:120]
            if not text:
                continue
            if name not in names:
                names.append(name)
            dialogues.append({"name": name, "text": text})
        if len(dialogues) < 3:
            continue
        if len({d["name"] for d in dialogues}) < 2:
            continue

        used.update(ids)
        first_text = _display_text(selected[0].get("text"))
        topic = str(raw.get("topic") or "").strip()[:28]
        if not topic:
            topic = f"{names[0]}：{first_text[:16]}"
        desc = str(raw.get("desc") or "").strip()[:80]
        if not desc:
            desc = f"{'、'.join(names[:3])} 在聊“{first_text[:24]}”"
        elif not _desc_is_grounded(desc, dialogues, source_names):
            desc = _factual_desc(dialogues)
        start = float(selected[0]["ts"])
        end = float(selected[-1]["ts"])
        # 热度看“这几句话前后 90 秒”的真实发言，而不是把两个选中片段中间夹着的
        # 十几分钟无关消息全算进来；这样徽章仍然能横向比较，又不会虚高。
        selected_ts = [float(m["ts"]) for m in selected]
        active = [
            m
            for m in source
            if any(abs(float(m["ts"]) - ts) <= heat_window_seconds for ts in selected_ts)
        ] or selected
        people = len({_uid(m) for m in active})
        hotspot = Hotspot(
            index=len(events) + 1,
            start=start,
            end=end,
            messages=active,
            heat=len(active),
            participants=people,
            score=people * 4 + min(len(active), 40),
        )
        events.append(
            {
                "topic": topic,
                "desc": desc,
                "dialogues": dialogues,
                "ids": ids,
                "heat": hotspot.heat,
                "participants": hotspot.participants,
                "score": hotspot.score,
                "time": hotspot.time_range,
            }
        )

    if not events:
        return None
    story["events"] = events
    story["group"] = group_name
    title = str(story.get("title") or "").strip()
    if not title or "群聊" in title or len(title) > 30:
        title = f"{group_name}今日趣事"
    story["title"] = title[:30]
    story["time"] = time.strftime("%m-%d %H:%M")
    return story


def _uid(m: dict) -> str:
    return str(m.get("uid") or m.get("name") or "")


def _laugh_score(messages: list[dict]) -> int:
    marks = ("哈", "笑死", "草", "绷", "乐", "？", "?", "！", "!")
    text = "\n".join(str(m.get("text") or "") for m in messages)
    return min(12, sum(text.count(k) for k in marks))


def find_hotspots(
    messages: list[dict],
    window_seconds: int = 300,
    step_seconds: int = 60,
    limit: int = 6,
    min_messages: int = 3,
    min_participants: int = 2,
) -> list[Hotspot]:
    """滑窗找热闹片段。

    先严格按“至少 3 条 + 至少 2 人”筛；不够再逐级放宽，保证小群也有候选。
    打分 = 参与人数 * 4 + min(消息条数, 40) + 笑声/问号/感叹号加成。
    选中的候选之间至少错开 60% 窗口，避免同一段被重复选中。
    """
    items = [m for m in messages if m.get("ts") and str(m.get("text") or "").strip()]
    if not items:
        return []
    items.sort(key=lambda m: float(m["ts"]))
    start0 = float(items[0]["ts"])
    last = float(items[-1]["ts"])
    if last - start0 <= window_seconds:
        starts = [start0]
    else:
        starts = list(range(int(start0), int(last) + 1, max(15, step_seconds)))

    def collect(min_msgs: int, min_people: int) -> list[Hotspot]:
        found: list[Hotspot] = []
        for st in starts:
            st = float(st)
            bucket = [m for m in items if st <= float(m["ts"]) < st + window_seconds]
            if len(bucket) < min_msgs:
                continue
            people = len({_uid(m) for m in bucket})
            if people < min_people:
                continue
            heat = len(bucket)
            score = people * 4 + min(heat, 40) + _laugh_score(bucket)
            found.append(
                Hotspot(
                    index=0,
                    start=st,
                    end=st + window_seconds,
                    messages=bucket,
                    heat=heat,
                    participants=people,
                    score=score,
                )
            )
        return found

    candidates = collect(min_messages, min_participants)
    if not candidates:
        candidates = collect(2, 1)
    if not candidates:
        candidates = collect(1, 1)
    if not candidates:
        return []

    candidates.sort(key=lambda h: (h.score, h.heat, h.participants), reverse=True)
    selected: list[Hotspot] = []
    for cand in candidates:
        if any(abs(cand.start - s.start) < window_seconds * 0.6 for s in selected):
            continue
        selected.append(cand)
        if len(selected) >= limit:
            break
    # 极端情况：全都在同一个窗口，至少保留分数最高的一条
    if not selected:
        selected = candidates[:1]
    for i, h in enumerate(selected, 1):
        h.index = i
    return selected


def fallback_story(hotspots: list[Hotspot], group: str = "本群") -> dict:
    """LLM 失败时的兜底：直接用候选里的原话，不编造。"""
    events = []
    for h in hotspots[:3]:
        picked: list[dict] = []
        used: set[str] = set()
        for m in reversed(h.messages):
            name = str(m.get("name") or "群友")
            text = _display_text(m.get("text"))
            if not text or name in used:
                continue
            used.add(name)
            picked.append({"name": name, "text": text[:120]})
            if len(picked) >= 3:
                break
        picked.reverse()
        # 兜底时别让每件事都叫“群里最热闹的 5 分钟”；拿第一句有内容的原话当标题，
        # 纯图片/表情就退回按时间窗命名。
        snippet = ""
        for m in h.messages:
            text = _display_text(m.get("text"))
            if text:
                snippet = text
                break
        if snippet:
            topic = "群里在聊：" + (snippet[:18] + "…" if len(snippet) > 18 else snippet)
        else:
            topic = f"群里最热闹的 {max(1, round((h.end - h.start) / 60))} 分钟"
        events.append(
            {
                "candidate": h.index,
                "topic": topic,
                "desc": f"{h.heat} 条消息、{h.participants} 人参与，聊得最热的一段。",
                "dialogues": picked,
                "heat": h.heat,
                "participants": h.participants,
                "score": h.score,
                "time": h.time_range,
            }
        )
    return {
        "title": f"{group}今日趣事",
        "group": group,
        "time": time.strftime("%m-%d %H:%M"),
        "events": events,
    }
