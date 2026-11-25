# -*- coding: utf-8 -*-
"""astrbot_plugin_group_summary —— 群趣事总结 v0.3.2。

后台缓存群消息；有人 @机器人 说「总结 / 趣事 / 好玩的事」时：
1. 把最近消息按 m001…编号整体交给 LLM，让它挑 2~3 段话题连贯的对话；
2. LLM 只返回消息编号，插件按编号取原话，校验跨度并过滤纯媒体，避免改写/跨话题硬拼；
3. 下载群成员真实头像，用 Pillow 画成聊天截图风漫画；
4. 只 yield 一张 PNG（图内有标题/时间/热度/人数），不再另发文字摘要。

用法（群聊）：
    @机器人 消息总结
    @机器人 群总结 / 最近有啥趣事
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import time
from collections import defaultdict, deque
from pathlib import Path

import httpx
from PIL import Image

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star, StarTools, register

from .analysis import (
    Hotspot,
    build_transcript,
    fallback_story,
    find_hotspots,
    normalize_selection,
)
from .local_history import (
    DEFAULT_GROUP_OPENID,
    DEFAULT_GROUP_QQ,
    load_group_history,
    merge_messages,
)
from .render import render_summary

PLUGIN_NAME = "astrbot_plugin_group_summary"

SUMMARY_KEYWORDS = ("总结", "群总结", "趣事", "好玩的事", "乐子")

SYSTEM_PROMPT = (
    "你是群聊编辑，专门从真实聊天记录里挑出连贯、好笑的片段，写成群趣事。"
    "你只能选择消息编号，不能改写、补写、虚构；渲染时原话会按编号从聊天记录里取。"
    "只输出一个 JSON，不要任何解释、Markdown 或代码块。"
)

MAX_DIALOGUE = 4


@register(PLUGIN_NAME, "astrbot-lele", "群趣事总结：LLM 按话题连贯性选消息并校验原话，生成带真实头像的聊天漫画", "0.3.2")
class GroupSummaryPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        max_msgs = max(20, int(self.config.get("max_msgs", 120) or 120))
        self._window = float(self.config.get("window_minutes", 120) or 120) * 60
        self._hotspot_window = max(
            60, int(self.config.get("hotspot_window_minutes", 5) or 5) * 60
        )
        self._max_events = max(1, min(3, int(self.config.get("max_events", 3) or 3)))
        # 不再只给 LLM 4 个固定时间窗；把最近消息带编号整体交给它挑连贯片段。
        self._selection_max_messages = max(
            60, min(500, int(self.config.get("selection_max_messages", 260) or 260))
        )
        # 总结是“顺手一看”的功能，不能为了等模型把群晾 30 秒；超时就用候选原话兜底。
        self._llm_timeout = max(
            5.0, min(60.0, float(self.config.get("llm_timeout_seconds", 15) or 15))
        )
        self._enabled_groups = {
            str(g) for g in (self.config.get("enabled_groups") or [])
        }
        # 本机 QQ NT 消息源：只读指定群，触发时补全 bot 没缓存到的历史。
        self._local_qq_enabled = bool(self.config.get("local_qq_enabled", True))
        self._local_qq_group_qq = int(
            self.config.get("local_qq_group_qq", DEFAULT_GROUP_QQ)
            or DEFAULT_GROUP_QQ
        )
        self._local_qq_group_openid = str(
            self.config.get("local_qq_group_openid", DEFAULT_GROUP_OPENID) or ""
        )
        self._local_qq_limit = max(
            50, min(2000, int(self.config.get("local_qq_limit", 800) or 800))
        )
        self._local_qq_mcp = str(self.config.get("local_qq_mcp", "") or "")
        self._local_qq_node = str(self.config.get("local_qq_node", "") or "")
        self._local_qq_key_file = str(
            self.config.get("local_qq_key_file", "") or ""
        )
        if self._local_qq_enabled:
            logger.info(
                "[group_summary] 本地消息源启用：群 %s，最近 %s 分钟",
                self._local_qq_group_qq,
                int(self._window // 60),
            )
        self.buffer: dict[str, deque] = defaultdict(lambda: deque(maxlen=max_msgs))
        self._group_meta: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._summary_lock = asyncio.Lock()
        self._data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self._avatar_dir = self._data_dir / "avatars"
        # QQ 官方群消息通常不带 author.avatar，只能拿到 member_openid（不能反查 QQ）。
        # 如果本机其他插件已经按昵称缓存过头像，就复用这些本地文件，避免全变成首字。
        self._local_avatars = self._scan_local_avatars()

    # ------------------------------------------------------------------ 缓存
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = str(event.get_group_id() or "")
        if not group_id:
            return
        if self._enabled_groups and group_id not in self._enabled_groups:
            return

        uid = str(event.get_sender_id() or "")
        if uid and uid == str(event.get_self_id() or ""):
            return
        name = self._sender_name(event, uid)
        text = (event.message_str or "").strip()
        if not text:
            text = "[图片/表情/非文本消息]"

        meta = self._read_group_meta(event)
        if meta.get("name") or meta.get("avatar"):
            old = self._group_meta.get(group_id, {})
            old.update({k: v for k, v in meta.items() if v})
            self._group_meta[group_id] = old

        record = {
            "uid": uid,
            "name": name,
            "text": text[:300],
            "ts": time.time(),
            "avatar": self._sender_avatar(event, uid),
        }
        trigger = self._is_mentioned_bot(event) and any(
            k in text for k in SUMMARY_KEYWORDS
        )
        # 触发语本身（“@乐乐 消息总结”）不算趣闻素材，别让总结里出现“消息总结”这段。
        if not trigger:
            async with self._lock:
                self.buffer[group_id].append(record)

        if trigger:
            event.stop_event()
            async for ret in self._handle_summary(event, group_id):
                yield ret

    def _is_mentioned_bot(self, event: AstrMessageEvent) -> bool:
        self_id = event.get_self_id()
        for m in event.get_messages():
            if isinstance(m, At):
                qq = getattr(m, "qq", None)
                if qq is None or str(qq) == str(self_id):
                    return True
        return False

    # ------------------------------------------------------------ 元信息/头像
    @staticmethod
    def _sender_name(event: AstrMessageEvent, uid: str) -> str:
        name = (event.get_sender_name() or "").strip()
        if not name or name == uid:
            raw = getattr(event.message_obj, "raw_message", None)
            author = getattr(raw, "author", None)
            name = (getattr(author, "username", "") or "").strip()
        if not name or name == uid:
            name = f"群友{uid[-4:]}" if uid else "群友"
        return name

    def _read_group_meta(self, event: AstrMessageEvent) -> dict:
        group = getattr(event.message_obj, "group", None)
        name = getattr(group, "group_name", None) if group else None
        avatar = getattr(group, "group_avatar", None) if group else None
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            data = raw.get("d") if isinstance(raw.get("d"), dict) else raw
            name = name or data.get("group_name")
            avatar = avatar or data.get("group_avatar")
        raw_data = getattr(raw, "raw_data", None)
        if not name and isinstance(raw_data, dict):
            name = raw_data.get("group_name")
        return {"name": str(name) if name else "", "avatar": str(avatar) if avatar else ""}

    @staticmethod
    def _sender_avatar(event: AstrMessageEvent, uid: str) -> str:
        raw = getattr(event.message_obj, "raw_message", None)
        author = getattr(raw, "author", None)
        url = getattr(author, "avatar", None) if author is not None else None
        raw_data = getattr(raw, "raw_data", None)
        if not url and isinstance(raw_data, dict):
            author_d = (
                raw_data.get("author")
                if isinstance(raw_data.get("author"), dict)
                else {}
            )
            url = author_d.get("avatar")
        if not url and isinstance(raw, dict):
            data = raw.get("d") if isinstance(raw.get("d"), dict) else raw
            author_d = data.get("author") if isinstance(data.get("author"), dict) else {}
            sender_d = data.get("sender") if isinstance(data.get("sender"), dict) else {}
            url = author_d.get("avatar") or sender_d.get("avatar")
        if not url:
            platform = (event.get_platform_name() or "").lower()
            if platform in {"aiocqhttp", "onebot", "qq"} and uid.isdigit():
                url = f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"
        return str(url) if url else ""

    async def _group_name(self, event: AstrMessageEvent, group_id: str) -> str:
        meta = self._group_meta.get(group_id, {})
        name = str(meta.get("name") or "").strip()
        if name:
            return name
        try:
            group = await event.get_group(group_id)
            name = str(getattr(group, "group_name", "") or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
        return name or "本群"

    def _scan_local_avatars(self) -> dict[str, str]:
        """扫描本机插件目录里按昵称命名的头像，作为 QQ 官方无头像时的兜底。"""
        roots = []
        try:
            roots.append(Path(__file__).resolve().parent.parent)
        except OSError:
            pass
        roots.append(self._data_dir.parent)
        found: dict[str, str] = {}
        exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        for root in roots:
            try:
                avatar_dirs = list(root.glob("*/avatars"))
            except OSError:
                continue
            for av_dir in avatar_dirs:
                if not av_dir.is_dir():
                    continue
                try:
                    files = list(av_dir.iterdir())
                except OSError:
                    continue
                for path in files:
                    if (
                        path.suffix.lower() in exts
                        and path.is_file()
                        and path.stat().st_size > 0
                    ):
                        found.setdefault(path.stem.strip(), str(path))
        if found:
            logger.info(f"[group_summary] 复用本地头像 {len(found)} 张")
        return found

    async def _download_avatars(self, msgs: list[dict]) -> dict[str, str]:
        """按昵称取真实头像：优先 URL，失败则用本地同名头像，最后才首字兜底。"""
        names = {
            str(m.get("name") or "").strip()
            for m in msgs
            if str(m.get("name") or "").strip()
        }
        result = {
            name: self._local_avatars[name]
            for name in names
            if name in self._local_avatars
        }
        urls: dict[str, str] = {}
        for m in msgs:
            name = str(m.get("name") or "").strip()
            url = str(m.get("avatar") or "").strip()
            if name and url and name not in urls:
                urls[name] = url
        if not urls:
            return result
        try:
            self._avatar_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return result

        sem = asyncio.Semaphore(6)
        async with httpx.AsyncClient(
            timeout=4.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
        ) as client:

            async def fetch(name: str, url: str) -> None:
                path = self._avatar_dir / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".img")
                if path.is_file() and path.stat().st_size > 0:
                    try:
                        with Image.open(path) as im:
                            im.verify()
                    except Exception:  # noqa: BLE001
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    else:
                        result[name] = str(path)
                        return
                async with sem:
                    try:
                        resp = await client.get(url)
                        if resp.status_code != 200 or not resp.content:
                            return
                        # 先验证是图片，避免把 HTML/错误页塞进缓存
                        with Image.open(io.BytesIO(resp.content)) as im:
                            im.verify()
                        path.write_bytes(resp.content)
                        result[name] = str(path)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"[group_summary] 头像下载失败 {name}: {exc}")

            await asyncio.gather(*(fetch(n, u) for n, u in urls.items()))
        return result

    # ---------------------------------------------------------------- 生成/发送
    async def _collect(self, group_id: str) -> list[dict]:
        async with self._lock:
            items = list(self.buffer.get(group_id, []))
        cutoff = time.time() - self._window
        return [m for m in items if float(m["ts"]) >= cutoff]

    async def _with_local_history(self, group_id: str, msgs: list[dict]) -> list[dict]:
        """只读本机 QQ 群历史，补全 bot 缓存；任何异常都回退到原缓存。"""
        if not self._local_qq_enabled:
            return msgs
        if self._local_qq_group_openid and group_id != self._local_qq_group_openid:
            return msgs
        if self._local_qq_group_qq <= 0:
            return msgs
        started = time.perf_counter()
        try:
            # 本地解密偶尔会被 QQ 写库拖慢；总结不能无限等，超时就只用 bot 缓存。
            local = await asyncio.wait_for(
                load_group_history(
                    self._local_qq_group_qq,
                    window_seconds=self._window,
                    limit=self._local_qq_limit,
                    mcp_path=self._local_qq_mcp,
                    node_path=self._local_qq_node,
                    key_file=self._local_qq_key_file,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[group_summary] 本地消息读取超时（%.1fs），回退 bot 缓存",
                time.perf_counter() - started,
            )
            return msgs
        except Exception as exc:  # noqa: BLE001
            logger.warning("[group_summary] 本地消息读取失败，回退 bot 缓存: %s", exc)
            return msgs
        if not local:
            return msgs
        merged = merge_messages(msgs, local, limit=self._local_qq_limit)
        logger.info(
            "[group_summary] 本地消息补全：%s 条（bot 缓存 %s 条）",
            len(local),
            len(msgs),
        )
        return merged

    async def _handle_summary(self, event: AstrMessageEvent, group_id: str):
        if self._summary_lock.locked():
            yield event.plain_result("刚总结完啦，让群里再热闹一会我再出手~")
            return
        async with self._summary_lock:
            started = time.perf_counter()
            t_local = t_llm = t_avatar = t_render = 0.0
            avatar_task: asyncio.Task | None = None
            try:
                msgs = await self._collect(group_id)
                msgs = await self._with_local_history(group_id, msgs)
                t_local = time.perf_counter() - started
                if len(msgs) < 2:
                    yield event.plain_result("最近群里太安静了，我没抓到什么消息，等热闹点再来~")
                    return
                group_name = await self._group_name(event, group_id)
                # 4 个候选足够挑 2~3 个故事，prompt 更短，模型回得更快。
                hotspots = find_hotspots(
                    msgs,
                    window_seconds=self._hotspot_window,
                    limit=4,
                )
                if not hotspots:
                    yield event.plain_result("最近消息太少，凑不出一段热闹的，等会再来~")
                    return

                # 头像和 LLM 并行：候选里的发言人先预热，故事里真正用到时通常已缓存。
                avatar_started = time.perf_counter()
                avatar_ready_at = 0.0

                async def prefetch_avatars():
                    nonlocal avatar_ready_at
                    result = await self._download_avatars(
                        [m for h in hotspots for m in h.messages]
                    )
                    avatar_ready_at = time.perf_counter()
                    return result

                avatar_task = asyncio.create_task(prefetch_avatars())
                provider_id = await self.context.get_current_chat_provider_id(
                    event.unified_msg_origin
                )
                t_llm_started = time.perf_counter()
                try:
                    story = await asyncio.wait_for(
                        self._generate(provider_id, msgs, group_name),
                        timeout=self._llm_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[group_summary] LLM 超时（%.1fs），改用候选原话兜底",
                        self._llm_timeout,
                    )
                    story = None
                t_llm = time.perf_counter() - t_llm_started
                if not story:
                    story = fallback_story(hotspots, group_name)
                story["group"] = group_name
                story["window_minutes"] = int(self._window // 60)
                story["total_messages"] = len(msgs)
                story["total_people"] = len({str(m.get("uid") or m.get("name")) for m in msgs})

                wanted = {
                    str(d.get("name") or "")
                    for ev in (story.get("events") or [])
                    for d in (ev.get("dialogues") or [])
                }
                avatar_msgs = [m for m in msgs if str(m.get("name") or "") in wanted]
                avatars = await avatar_task
                t_avatar = max(0.0, avatar_ready_at - avatar_started)
                # 故事里若有候选没覆盖到的人，再补一次；已有缓存时只做本地读取。
                if any(str(m.get("name") or "") not in avatars for m in avatar_msgs):
                    extra_started = time.perf_counter()
                    avatars.update(await self._download_avatars(avatar_msgs))
                    t_avatar += time.perf_counter() - extra_started
                data_dir = self._data_dir
                data_dir.mkdir(parents=True, exist_ok=True)
                out = data_dir / f"summary_{int(time.time())}.png"
                t_render_started = time.perf_counter()
                render_summary(story, str(out), avatars=avatars)
                t_render = time.perf_counter() - t_render_started

                logger.info(
                    "[group_summary] 完成：总 %.1fs｜本地 %.1fs｜LLM %.1fs｜头像 %.1fs｜渲染 %.1fs｜%d 条",
                    time.perf_counter() - started,
                    t_local,
                    t_llm,
                    t_avatar,
                    t_render,
                    len(msgs),
                )
                # 只发一张图：图里已经有标题、热度、参与人数和对话，避免刷一串文字。
                # AstrBot 的事件结果只保留一次 yield；文字+图片分两次 yield 时图片会被丢掉。
                yield event.image_result(str(out))
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[group_summary] 总结失败: {exc}")
                yield event.plain_result("总结失败啦，等会再试一次~")
            finally:
                # 渲染/发送提前失败时别把后台头像任务留成孤儿。
                if avatar_task is not None and not avatar_task.done():
                    avatar_task.cancel()

    async def _generate(
        self,
        provider_id: str,
        msgs: list[dict],
        group_name: str,
    ) -> dict | None:
        source, transcript = build_transcript(
            msgs, max_messages=self._selection_max_messages
        )
        if not transcript:
            return None
        prompt = (
            f"群名：{group_name}\n"
            f"统计窗口：最近 {int(self._window // 60)} 分钟，"
            f"这里按时间顺序给了 {len(source)} 条消息。\n\n"
            "【怎么挑】\n"
            "1. 挑 2~3 段最有趣、彼此独立、话题连贯的对话；一段只能讲一个话题。"
            "消息超过 30 条时不要只写 1 段，至少再扫一遍有没有第二段有来有回的对话。\n"
            "2. 如果同一段里明显有两个话题，只取其中一个，不要硬拼；宁可不写，也不要凑数。\n"
            "3. 每段选 3~4 条消息编号，必须能前后连起来（首尾相隔不超过 12 条）；"
            "只选编号，不要复制原文。\n"
            "4. 每段至少有 2 个不同的人说话，最好有来有回；不要选一个人连续刷屏，"
            "不要选纯链接/图片/视频/表情/XML，不要选纯“这是谁”或只有一两个字的碎片。\n"
            "5. topic 要具体（谁/聊了什么），不要用“考古现场/全民侦探/计划”这种夸张概括；"
            "desc 用一句话说清“谁聊了什么、结论或笑点”，但只能写 ids 这几条里能直接看出来的事实："
            "不能加原文没有的动机、评价，也不能写没选中的后续结论；需要结论就把它选进 ids。\n"
            "6. 不要重复选同一段；不要虚构没出现的消息编号。\n\n"
            "【输出格式】只输出 JSON：\n"
            '{"title":"羊村今日趣事","events":['
            '{"topic":"具体事件","desc":"一句话事实+笑点","ids":["m012","m013","m014"]}'
            "]}\n\n"
            "【例子】\n"
            '好：topic=“M.2固态没插槽”，desc=“ashearh买了M.2发现主板没槽，'
            '.o.吐槽他买前不看主板。”\n'
            '坏：topic=“SATA考古现场与作业推销员”，把 SATA 和作业两个话题硬拼在一起。\n\n'
            "【消息】\n"
            f"{transcript}\n\n"
            "现在请只输出 JSON。"
        )
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT,
            )
            text = llm_resp.completion_text or ""
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[group_summary] llm_generate 失败: {exc}")
            return None
        story = self._parse_json(text)
        if not story:
            logger.warning("[group_summary] LLM 返回不是 JSON，长度 %d", len(text))
            return None
        normalized = normalize_selection(
            story,
            source,
            group_name,
            max_events=self._max_events,
            max_span_messages=60,
            max_span_seconds=1800,
        )
        if not normalized:
            logger.warning("[group_summary] LLM 选中的编号无效，改用候选兜底")
            return None
        logger.info(
            "[group_summary] LLM 选中：%s",
            "；".join(
                f"{ev.get('topic')}（"
                f"{','.join(f'm{i:03d}' for i in ev.get('ids') or [])}）"
                for ev in normalized.get("events") or []
            ),
        )
        return normalized

    @staticmethod
    def _pick_candidate(value, by_index: dict[int, Hotspot], used: set[int]) -> Hotspot | None:
        try:
            idx = int(value)
        except (TypeError, ValueError):
            idx = 0
        if idx in by_index and idx not in used:
            return by_index[idx]
        for i in sorted(by_index):
            if i not in used:
                return by_index[i]
        return None

    @staticmethod
    def _match_name(name: str, allowed: set[str]) -> str:
        if not name:
            return ""
        if name in allowed:
            return name
        for real in allowed:
            if name in real or real in name:
                return real
        return ""

    @staticmethod
    def _fallback_dialogues(h: Hotspot) -> list[dict]:
        picked: list[dict] = []
        used: set[str] = set()
        for m in reversed(h.messages):
            name = str(m.get("name") or "群友")
            text = str(m.get("text") or "").strip()
            if not text or name in used:
                continue
            used.add(name)
            picked.append({"name": name, "text": text[:120]})
            if len(picked) >= 3:
                break
        picked.reverse()
        return picked

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        text = text.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                try:
                    data = json.loads(m.group())
                    return data if isinstance(data, dict) else None
                except Exception:  # noqa: BLE001
                    return None
        return None
