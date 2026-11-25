# -*- coding: utf-8 -*-
"""astrbot_plugin_dsh_bridge —— 通过 QQ 让本机 DeepSeek Harness 干活。

链路：
    /dsh <任务>（私聊管理员）
      → 找本机 DeepSeek Harness 的 headless CLI
      → 注入 AstrBot 当前 provider 的 key/base_url
      → dsh --profile headless [--session-id <id>] "<任务>"
      → 把最终输出发回 QQ

会话：
    /dsh sessions  列出 DSH 最近的对话（带工作区）
    /dsh use N     选中一个，之后任务续在它的历史里
    /dsh new       取消选中，回到每次新建

安全取舍：
- 只允许管理员，且只允许私聊；群里会公开任务和结果。
- 不提供任意 shell 入口，但 DSH 本身能读写文件/执行命令，等于间接控制本机。
- 单次超时、输出截断、审计日志；同一时间只跑一个任务。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

from .dsh_runner import (
    DEFAULT_HEARTBEAT,
    DEFAULT_TIMEOUT,
    MAX_HANDOFF,
    MAX_OUTPUT,
    MAX_TASK,
    SESSION_ID_RE,
    append_audit,
    build_handoff_task,
    find_install,
    format_heartbeat,
    format_result,
    heartbeat_interval,
    is_noise_line,
    list_sessions,
    pick_new_session,
    reasoning_interval,
    reasoning_max_reports,
    run_task,
    select_provider,
    session_handoff,
    session_info,
    task_timeout,
    workspace_name,
)

PLUGIN_NAME = "astrbot_plugin_dsh_bridge"
CONFIG_PATH = Path.home() / ".astrbot" / "data" / "cmd_config.json"

HELP = f"""dsh · 本机 DeepSeek Harness 远程任务桥

用法（仅管理员 + 私聊）：
  /dsh <任务>        交给本机 DSH headless 跑，返回最终结果
  /dsh sessions      列出最近对话（含工作区、更新时间）
  /dsh use 2         选中第 2 个；之后任务续在它的历史里
  /dsh use session-… 直接按会话 ID 选
  /dsh new           取消选中，回到每次新建
  /dsh status        查看桥状态（不跑任务）
  /dsh stop          终止正在跑的任务
  /dsh help          看这段说明

例子：
  /dsh sessions
  /dsh use 1
  /dsh 继续刚才的活，只改测试
  /dsh 只回复 pong
  /dsh 看一下当前工作区有哪些文件，不要改

等待与报告：
- 单次默认等 {DEFAULT_TIMEOUT // 60} 分钟，可用 DSH_BRIDGE_TIMEOUT 改（秒）。
- 运行中每 {DEFAULT_HEARTBEAT} 秒报告一次“还在跑/已用多久/进程是否存活”；DSH_BRIDGE_HEARTBEAT=0 关闭。
- 推理摘录默认每 {reasoning_interval()} 秒最多一条、一次任务最多 {reasoning_max_reports()} 条；可用 DSH_BRIDGE_REASONING_INTERVAL / DSH_BRIDGE_REASONING_MAX 改（MAX=0 关闭）。
- 心跳报告进程状态；推理摘录来自 stderr，可能滞后/不完整，结束还会给一段摘录。

注意：
- 续会话会带上前文，token 更多，也更容易被旧上下文带偏；不确定就 /dsh new。
- 选中的会话如果是非 Default 的 agent preset（如 cordis/standard），会自动新建 Default 会话，并带最近几轮摘要继续，不需要再去 GUI 改设置；成功后会自动选中新会话。
- ⏳ 标记表示 5 分钟内更新过，可能正在 GUI 里用；同时写可能冲突。
- DSH 以你的 Windows 账户执行，能读写文件、跑命令；这条桥等于远程控制电脑。
- 输出截断 6000 字，任务最多 2000 字；自动接续的旧上下文最多约 {MAX_HANDOFF} 字。
- 不在群里用；任务和结果会写审计日志（不存完整输出）。"""


def default_workdir() -> Path:
    env = os.environ.get("DSH_BRIDGE_WORKDIR")
    if env:
        return Path(env).expanduser()
    p = Path.home() / "Documents" / "deepseek-harness" / "default-workspace"
    return p if p.is_dir() else Path.home()


def real_agent_preset(session_id: str) -> str | None:
    """读会话 zstd 日志第一帧 header 的真实 agentPreset；None 表示没有（可续）。

    projcache 的 JSON 是 GUI 缓存，可能与真实会话状态不一致；headless 续跑
    依据的是 zstd 会话日志里的 header，所以这里直接读它判断。
    """
    try:
        import zstandard as zstd
        import glob as _glob
        import json as _json
    except Exception:  # noqa: BLE001
        return None
    try:
        base = Path.home() / ".dsh" / "sessions"
        cand = _glob.glob(str(base / "*" / session_id / "session.v4.jsonl.zstd"))
        if not cand:
            return None
        d = zstd.ZstdDecompressor()
        with open(cand[0], "rb") as fh:
            with d.stream_reader(fh) as r:
                line = b""
                while True:
                    c = r.read(1)
                    if not c:
                        break
                    line += c
                    if c == b"\n":
                        break
        return _json.loads(line.decode("utf-8", "replace")).get("agentPreset")
    except Exception:  # noqa: BLE001
        return None


def read_provider_credentials() -> tuple[str, str, str]:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.warning(f"[dsh] 读 provider 配置失败: {exc}")
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    key, base, label = select_provider(cfg)
    if key:
        return key, base, label

    # 配置里没有可用 chat provider 时，允许直接给 AstrBot 进程设置
    # DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL；dsh_runner 会把它们注入子进程。
    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    env_base = os.environ.get("DEEPSEEK_BASE_URL", "").strip()
    if env_key:
        return env_key, env_base.rstrip("/"), "env/DEEPSEEK_API_KEY"
    return "", "", ""


@register(
    PLUGIN_NAME,
    "astrbot-lele",
    "通过 QQ 私聊让本机 DeepSeek Harness 以 headless 模式跑任务：可选已有对话续写（/dsh sessions、/dsh use N），默认等 1 小时，运行中每 60 秒报告存活，推理摘录限频；仅管理员、仅私聊，带超时/截断/审计。",
    "0.3.1",
)
class DshBridge(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._busy = False
        self._current_stop: asyncio.Event | None = None
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._audit = data_dir / "audit.jsonl"
        self._state_path = data_dir / "state.json"
        self._state = self._load_state()
        self._last_sessions: dict[str, list[dict]] = {}
        logger.info(f"[dsh] 审计日志：{self._audit}")

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"selected": {}}
        if not isinstance(data, dict):
            return {"selected": {}}
        selected = data.get("selected")
        if not isinstance(selected, dict):
            selected = {}
        return {"selected": selected}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(f"[dsh] 保存会话选择失败: {exc}")

    def _selection(self, sender: str) -> dict | None:
        raw = self._state.get("selected", {}).get(str(sender))
        if not isinstance(raw, dict):
            return None
        if not SESSION_ID_RE.match(str(raw.get("session_id") or "")):
            return None
        raw.setdefault("title", "(无标题)")
        raw.setdefault("cwd", "")
        return raw

    def _set_selection(self, sender: str, info: dict) -> None:
        self._state.setdefault("selected", {})[str(sender)] = {
            "session_id": info["id"],
            "cwd": info.get("cwd") or "",
            "title": info.get("title") or "(无标题)",
        }
        self._save_state()

    def _clear_selection(self, sender: str) -> None:
        self._state.setdefault("selected", {}).pop(str(sender), None)
        self._save_state()

    @filter.command("dsh", alias={"远程任务"})
    async def dsh_cmd(self, event: AstrMessageEvent, args: GreedyStr):
        """让本机 DSH 跑任务：/dsh <任务>；会话：sessions/use/new"""
        if not event.is_admin():
            yield event.plain_result("dsh 只对管理员开放。")
            return
        if not event.is_private_chat():
            yield event.plain_result("dsh 只允许私聊使用；群里会把任务和结果公开。")
            return

        sender = str(event.get_sender_id())
        task = str(args).strip()
        low = task.lower()
        if not task or low in {"help", "-h", "--help"}:
            yield event.plain_result(HELP)
            return
        if low == "status":
            yield event.plain_result(self._status_text(sender))
            return
        if low == "sessions":
            yield event.plain_result(self._sessions_text(sender))
            return
        if low == "new":
            self._clear_selection(sender)
            yield event.plain_result("已取消选中；之后 /dsh <任务> 每次都新建对话。")
            return
        if low == "use" or low.startswith("use "):
            yield event.plain_result(self._use_text(sender, task[3:].strip()))
            return
        if low == "stop":
            yield event.plain_result(self._stop_text())
            return
        if len(task) > MAX_TASK:
            yield event.plain_result(f"任务太长（{len(task)} 字），最多 {MAX_TASK} 字。")
            return
        if self._busy:
            yield event.plain_result("上一个 DSH 任务还在跑，等它结束再发。")
            return

        key, api_base, label = read_provider_credentials()
        if not key:
            yield event.plain_result(
                "没找到可用的模型 key（cmd_config.json 里当前 provider 没有 key）。\n"
                "先在 AstrBot 页面把 provider 配好，或给 DSH 单独设 DEEPSEEK_API_KEY。"
            )
            return

        selection = self._selection(sender)
        original_task = task
        session_id = selection["session_id"] if selection else None
        workdir = default_workdir()
        if selection:
            candidate = Path(str(selection.get("cwd") or ""))
            if candidate.is_dir():
                workdir = candidate
            else:
                logger.warning(f"[dsh] 会话工作区不存在，改用默认: {candidate}")

        # headless one-shot 不支持非 Default 的 agent preset（cordis/standard 等）。
        # 不再要求用户去 GUI 改设置：自动新建 Default 会话，并带一小段旧上下文接续。
        fallback_preset = ""
        original_title = selection.get("title") if selection else ""
        before_ids: set[str] = set()
        if selection:
            _rp = real_agent_preset(selection["session_id"])
            if _rp:
                fallback_preset = _rp
                handoff = session_handoff(selection["session_id"])
                task = build_handoff_task(
                    task,
                    original_title or "(无标题)",
                    _rp,
                    handoff,
                )
                session_id = None
                before_ids = {
                    s["id"]
                    for s in list_sessions(limit=100, include_archived=True)
                }
                logger.info(
                    f"[dsh] 会话 {selection['session_id']} 的 preset={_rp} 不能 headless，"
                    f"自动新建 Default 会话（旧上下文 {len(handoff)} 字）"
                )

        self._busy = True
        started = datetime.now()
        timeout = task_timeout()
        hb = heartbeat_interval()
        self._current_stop = asyncio.Event()
        reason_buf: list[str] = []
        last_flush = time.monotonic()
        reason_reports = 0
        reason_interval = reasoning_interval()
        reason_max = reasoning_max_reports()

        async def stream_line(txt: str) -> None:
            nonlocal last_flush, reason_reports
            t = txt.strip()
            if not t or t.startswith("dsh: reasoning:") or is_noise_line(t):
                return
            reason_buf.append(t)
            if len(reason_buf) > 60:
                del reason_buf[:-60]
            joined = "\n".join(reason_buf)
            if len(joined) > 1600:
                joined = "…\n" + joined[-1600:]
            if reason_reports >= reason_max:
                return
            now = time.monotonic()
            if (now - last_flush) < reason_interval:
                return
            if len(joined) < 60:
                return
            last_flush = now
            reason_reports += 1
            try:
                await event.send(
                    MessageChain().message(
                        f"🧠 推理摘录（{int(now - start_mono)}s）：\n{joined}"
                    )
                )
            except Exception:  # noqa: BLE001
                pass
            reason_buf.clear()

        start_mono = time.monotonic()
        try:
            try:
                if fallback_preset:
                    where = (
                        f"新建 Default 对话（原会话「{original_title or '(无标题)'}」的 "
                        f"{fallback_preset} 不能 headless，已自动带最近上下文）"
                    )
                else:
                    where = f"续在「{selection['title']}」" if selection else "新建对话"
                await event.send(
                    MessageChain().message(
                        f"收到，{where}，交给本机 DSH（{label or 'provider'}）跑，最多 {timeout} 秒（约 {timeout // 60} 分钟）…\n想停就发 /dsh stop"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[dsh] 发送回执失败: {exc}")

            async def progress(elapsed: float, alive: bool) -> None:
                await self._send_progress(event, selection, elapsed, alive)

            result = await run_task(
                task,
                key,
                api_base,
                timeout=timeout,
                workdir=workdir,
                session_id=session_id,
                heartbeat=hb,
                on_heartbeat=progress if hb else None,
                on_stderr=stream_line,
                stop_event=self._current_stop,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[dsh] 任务执行异常: {exc}")
            result = {"error": f"运行异常：{exc}", "code": -1}
        finally:
            self._busy = False
            self._current_stop = None
            if reason_buf:
                excerpt = "\n".join(reason_buf)[-1500:]
                try:
                    await event.send(
                        MessageChain().message(
                            "🧠 推理摘录（结束）：\n" + excerpt
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass
                reason_buf.clear()

        fallback_ok = bool(
            fallback_preset
            and not result.get("error")
            and not result.get("timeout")
            and not result.get("stopped")
        )
        new_info = None
        if fallback_ok:
            new_info = await self._find_new_session(before_ids, str(workdir))
            if new_info:
                # headless 给新会话生成的标题可能很长/乱码；选中态用可读的接续名。
                new_info = dict(new_info)
                base_title = (original_title or "新会话").strip() or "新会话"
                new_info["title"] = f"{base_title}（自动接续）"
                self._set_selection(sender, new_info)
                logger.info(f"[dsh] 自动接续到新会话 {new_info['id']}")

        append_audit(
            self._audit,
            {
                "ts": started.isoformat(timespec="seconds"),
                "sender": sender,
                "umo": event.unified_msg_origin,
                "session": (
                    new_info["id"]
                    if new_info
                    else (selection["session_id"] if fallback_preset and selection else session_id)
                )
                or "",
                "fallback_preset": fallback_preset,
                "fallback_ok": fallback_ok,
                "cwd": str(workdir),
                "task": original_task[:500],
                "code": result.get("code"),
                "seconds": result.get("seconds"),
                "timeout": bool(result.get("timeout")),
                "stopped": bool(result.get("stopped")),
            },
        )
        if result.get("stopped"):
            text = "⏹ 已手动终止（/dsh stop）。"
            if result.get("seconds") is not None:
                text += f"\n（已运行 {result.get('seconds')} 秒）"
        else:
            text = format_result(result)
            if not result.get("error") and not result.get("timeout"):
                text = f"{text}\n\n（用时 {result.get('seconds', 0)} 秒）"
        if fallback_preset:
            if new_info:
                text = (
                    f"（已自动新建并选中「{new_info['title']}」｜"
                    f"原会话 preset={fallback_preset} 不能 headless）\n{text}"
                )
            elif fallback_ok:
                text = (
                    f"（原会话 preset={fallback_preset} 不能 headless，"
                    "已用新 Default 会话跑完；没定位到新会话，下次仍会这样自动接）\n"
                    f"{text}"
                )
            else:
                text = (
                    f"（原会话 preset={fallback_preset} 不能 headless，"
                    "本次已尝试新 Default 会话但没成功，选中未改）\n"
                    f"{text}"
                )
        elif session_id:
            text = f"（续在：{selection['title']}）\n{text}"
        if len(text) > MAX_OUTPUT + 200:
            text = text[:MAX_OUTPUT] + "\n…（已截断）"
        yield event.plain_result(text)

    async def _find_new_session(
        self,
        before_ids: set[str],
        cwd: str,
    ) -> dict | None:
        """等新会话写入 projcache；最多等约 3 秒，不阻塞 QQ 太久。"""
        for _ in range(6):
            await asyncio.sleep(0.4)
            info = pick_new_session(
                before_ids,
                list_sessions(limit=100, include_archived=True),
                cwd,
            )
            if info:
                return info
        return None

    async def _send_progress(
        self,
        event: AstrMessageEvent,
        selection: dict | None,
        elapsed: float,
        alive: bool,
    ) -> None:
        prefix = f"「{selection['title']}」" if selection else ""
        await event.send(MessageChain().message(prefix + format_heartbeat(elapsed, alive)))

    def _sessions_text(self, sender: str) -> str:
        sessions = list_sessions(limit=10)
        self._last_sessions[sender] = sessions
        if not sessions:
            return "没在 DSH 缓存里找到对话。先在本机 GUI 里开一个，或直接 /dsh <任务> 新建。"
        lines = ["最近的 DSH 对话（按最后更新）："]
        for i, s in enumerate(sessions, 1):
            updated = s["updated_at"].strftime("%m-%d %H:%M") if s.get("updated_at") else "--"
            flag = " ⏳" if s.get("active") else ""
            lines.append(
                f"{i}. {s['title']}｜{workspace_name(s.get('cwd', ''))}｜{updated}{flag}"
            )
        lines += [
            "",
            "选中：/dsh use 序号（或 /dsh use session-id）",
            "取消：/dsh new",
            "⏳ = 5 分钟内更新过，可能正在别的窗口用。",
        ]
        return "\n".join(lines)

    def _use_text(self, sender: str, arg: str) -> str:
        if not arg:
            return "用法：/dsh use 序号；序号从 /dsh sessions 来。"
        info = None
        if arg.isdigit():
            cached = self._last_sessions.get(sender) or []
            idx = int(arg)
            if 1 <= idx <= len(cached):
                info = cached[idx - 1]
            else:
                return f"序号超出范围（当前列表 {len(cached)} 条）；先 /dsh sessions。"
        else:
            info = session_info(arg)
        if not info:
            return f"找不到会话：{arg}\n用 /dsh sessions 看列表，或 /dsh use session-… 直接指定 ID。"
        self._set_selection(sender, info)
        warn = ""
        if info.get("active"):
            warn = "\n⏳ 这个对话 5 分钟内更新过，可能正在 GUI 里用；同时写可能冲突。"
        return (
            f"已选中：{info['title']}\n"
            f"工作区：{info.get('cwd') or '(未知)'}\n"
            f"之后 /dsh <任务> 会续在这个对话里；/dsh new 取消。{warn}"
        )

    def _status_text(self, sender: str) -> str:
        found = find_install()
        key, api_base, label = read_provider_credentials()
        workdir = default_workdir()
        selection = self._selection(sender)
        hb = heartbeat_interval()
        report = f"每 {hb}s 报告" if hb else "不报告"
        lines = [
            "dsh 桥状态：",
            f"- CLI：{'找到' if found else '没找到 DeepSeek Harness'}",
            f"- 模型：{label or '无可用 provider'}",
            f"- key：{'已配置' if key else '缺失'}｜base_url：{'已配置' if api_base else '默认'}",
        ]
        if selection:
            lines += [
                f"- 当前会话：{selection['title']}｜{workspace_name(selection.get('cwd', ''))}",
                f"- 会话 ID：{selection['session_id']}",
                f"- 会话工作区：{selection.get('cwd') or '(未知)'}",
            ]
        else:
            lines.append("- 当前会话：未选中（每次 /dsh 新建）")
        lines += [
            f"- 默认工作目录：{workdir}",
            f"- 当前：{'忙碌' if self._busy else '空闲'}｜超时 {task_timeout()}s｜{report}｜输出上限 {MAX_OUTPUT} 字",
            f"- 推理摘录：每 {reasoning_interval()}s 最多 1 条，一次任务最多 {reasoning_max_reports()} 条",
            f"- 审计：{self._audit}",
        ]
        return "\n".join(lines)

    def _stop_text(self) -> str:
        if self._current_stop is None or self._current_stop.is_set():
            return "当前没有正在跑的 DSH 任务可终止。"
        self._current_stop.set()
        return "已发送终止指令，正在杀掉 DSH 子进程…"
