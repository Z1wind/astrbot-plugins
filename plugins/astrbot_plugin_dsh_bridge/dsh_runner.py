# -*- coding: utf-8 -*-
"""调用本机 DeepSeek Harness 的 headless 模式，跑一次性任务或续指定会话。

DSH 没有安装到 PATH，只能通过 Electron 可执行文件 + asar 里的 cli.js 调用：
    "DeepSeek Harness.exe" --expose-internals <cli.js> --profile headless "<task>"
续会话时多一个 --session-id <id>，会读到 desktop/web 里已有的对话历史。

headless 配置默认没有模型凭据，所以要把 AstrBot 当前 provider 的 key/base_url
注入子进程环境变量（DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL），只存在于子进程。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

DSH_DIR = Path("Programs") / "DeepSeek Harness"
CLI_REL = Path("resources") / "app.asar" / "dsh" / "node_modules" / "@deepseek-ai" / "dsh-desktop-host" / "lib" / "cli.js"
DEFAULT_TIMEOUT = 3600  # 默认 1 小时，避免长任务中途被杀；可用 DSH_BRIDGE_TIMEOUT 覆盖
DEFAULT_HEARTBEAT = 60
DEFAULT_REASONING_INTERVAL = 120  # 推理摘录最少隔 2 分钟一条
DEFAULT_REASONING_MAX = 4  # 一次任务最多发 4 条实时推理摘录
MAX_OUTPUT = 6000
MAX_TASK = 2000
MAX_HANDOFF = 1800  # 自动接续时最多带多少旧上下文
SESSION_ID_RE = re.compile(r"^session-[0-9a-fA-F-]{36}$")
ACTIVE_WINDOW = 300  # 秒；5 分钟内更新过的会话可能在 GUI 里正开着


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def task_timeout() -> int:
    """单次任务超时，默认 1 小时；DSH_BRIDGE_TIMEOUT 可覆盖（秒）。"""
    return _env_int("DSH_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT, 10)


def heartbeat_interval() -> int:
    """进度报告间隔，默认 60 秒；DSH_BRIDGE_HEARTBEAT=0 可关掉。"""
    return _env_int("DSH_BRIDGE_HEARTBEAT", DEFAULT_HEARTBEAT, 0)


def reasoning_interval() -> int:
    """推理摘录间隔，默认 120 秒；最小 15 秒。"""
    return _env_int("DSH_BRIDGE_REASONING_INTERVAL", DEFAULT_REASONING_INTERVAL, 15)


def reasoning_max_reports() -> int:
    """单次任务最多发几条实时推理摘录；0 表示不发实时摘录。"""
    return _env_int("DSH_BRIDGE_REASONING_MAX", DEFAULT_REASONING_MAX, 0)


def find_install() -> tuple[Path, str] | None:
    """返回 (可执行文件, asar 内 cli.js 路径)；找不到返回 None。

    cli.js 在 app.asar 虚拟目录里，Windows 的 Path.is_file() 看不到它，
    所以只校验 exe 和 app.asar 本身，cli 路径交给 Electron 解析。
    """
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local))
    roots.append(Path.home() / "AppData" / "Local")
    for root in roots:
        exe = root / DSH_DIR / "DeepSeek Harness.exe"
        asar = root / DSH_DIR / "resources" / "app.asar"
        if exe.is_file() and asar.is_file():
            return exe, str(root / DSH_DIR / CLI_REL)
    return None


_NON_CHAT_SOURCE_MARKERS = (
    "embedding",
    "rerank",
    "stt",
    "tts",
    "speech",
    "whisper",
    "audio",
    "moderation",
)


def _source_key(source: dict[str, Any]) -> str:
    """取 provider source 的 key，兼容 key/api_key 与多 key 列表。"""
    raw = source.get("key") or source.get("api_key") or ""
    if isinstance(raw, (list, tuple)):
        raw = next((item for item in raw if str(item).strip()), "")
    return str(raw).strip()


def _is_chat_source(source: dict[str, Any]) -> bool:
    """判断 source 是不是聊天模型；embedding/STT/TTS 不能拿去给 DSH 当模型。"""
    provider_type = str(source.get("provider_type") or "").strip().lower()
    if provider_type:
        return "chat" in provider_type
    source_type = str(source.get("type") or "").strip().lower()
    if not source_type:
        # 老配置可能没有 type，保留原来的宽松行为。
        return True
    return not any(marker in source_type for marker in _NON_CHAT_SOURCE_MARKERS)


def _label_for(source: dict[str, Any], provider: dict[str, Any] | None = None) -> str:
    source_id = str(source.get("id") or "")
    model = str(provider.get("model") or "") if isinstance(provider, dict) else ""
    return source_id + ("/" + model if model else "")


def select_provider(cfg: dict[str, Any]) -> tuple[str, str, str]:
    """从 cmd_config.json 里挑当前可用的聊天 provider，返回 (key, api_base, label)。

    不能直接取「第一个 enable 的 provider」：AstrBot 会同时启用 embedding
    provider（如 ollama_embedding/bge-m3），它没有 key，排在最前面时会误报
    “没有可用模型 key”。这里只接受带 key 的 chat_completion source。
    """
    providers = cfg.get("provider") or []
    sources = cfg.get("provider_sources") or []
    if not isinstance(providers, list):
        providers = []
    if not isinstance(sources, list):
        sources = []

    def resolve_source(provider: Any) -> dict[str, Any] | None:
        if not isinstance(provider, dict):
            return None
        sid = provider.get("provider_source_id")
        return next(
            (s for s in sources if isinstance(s, dict) and s.get("id") == sid),
            None,
        )

    # 1) 启用的聊天 provider，且有 key。
    for provider in providers:
        if not isinstance(provider, dict) or not provider.get("enable"):
            continue
        source = resolve_source(provider)
        if not isinstance(source, dict) or not _is_chat_source(source):
            continue
        key = _source_key(source)
        if not key:
            continue
        base = str(source.get("api_base") or source.get("base_url") or "").strip()
        return key, base.rstrip("/"), _label_for(source, provider)

    # 2) 没有启用的聊天 provider 时，回退到任意带 key 的聊天 source。
    for source in sources:
        if not isinstance(source, dict) or not _is_chat_source(source):
            continue
        key = _source_key(source)
        if not key:
            continue
        base = str(source.get("api_base") or source.get("base_url") or "").strip()
        return key, base.rstrip("/"), _label_for(source)

    return "", "", ""


def build_env(base: dict[str, str], key: str, api_base: str) -> dict[str, str]:
    env = dict(base)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    if key:
        env["DEEPSEEK_API_KEY"] = key
        # headless profile 现在用 llm-pi-ai/qianye；同一个 key 也作为
        # QIANYE_API_KEY 注入，兼容 DSH 的 qianye provider 配置。
        env["QIANYE_API_KEY"] = key
    if api_base:
        env["DEEPSEEK_BASE_URL"] = api_base
    return env


def session_cache_dir() -> Path:
    """DSH 把会话元数据缓存在这里，一个会话一个 json。"""
    return Path.home() / ".dsh" / "storages" / "session_projcache" / "sessions"


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _archived_session_ids() -> set[str]:
    data = _read_json(Path.home() / ".dsh" / "storages" / "workspace.json")
    if not isinstance(data, dict):
        return set()
    glob = data.get("global") or {}
    if not isinstance(glob, dict):
        return set()
    items = glob.get("archivedSessionIds") or []
    return {str(x) for x in items if str(x).strip()}


def _parse_session(path: Path) -> dict[str, Any] | None:
    sid = path.stem
    if not SESSION_ID_RE.match(sid):
        return None
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    record = data.get("record") or {}
    if not isinstance(record, dict):
        return None
    identity = record.get("identity") or {}
    rows = record.get("rows") or {}
    if not isinstance(identity, dict) or not isinstance(rows, dict):
        return None
    title = rows.get("title")
    if isinstance(title, dict):
        title = title.get("val")
    title = str(title or "").strip() or "(无标题)"
    cwd = str(identity.get("cwd") or "").strip()
    agent_preset = ""
    ap = rows.get("agentPreset")
    if isinstance(ap, dict):
        agent_preset = str(ap.get("val") or "").strip()
    created_ms = identity.get("createdAt")
    try:
        created = datetime.fromtimestamp(int(created_ms) / 1000)
    except (TypeError, ValueError, OSError, OverflowError):
        created = None
    try:
        updated = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        updated = None
    age = (time.time() - path.stat().st_mtime) if path.exists() else 10**9
    return {
        "id": sid,
        "title": title[:60],
        "cwd": cwd,
        "agent_preset": agent_preset,
        "created_at": created,
        "updated_at": updated,
        "active": age < ACTIVE_WINDOW,
    }


def session_info(session_id: str) -> dict[str, Any] | None:
    sid = (session_id or "").strip()
    if not SESSION_ID_RE.match(sid):
        return None
    return _parse_session(session_cache_dir() / f"{sid}.json")


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def session_handoff(
    session_id: str,
    max_turns: int = 2,
    max_chars: int = MAX_HANDOFF,
) -> str:
    """从 GUI 缓存里取最近几轮摘要，给自动新建的 Default 会话接上下文。

    只读 projcache，不改原会话；取不到就返回空串。
    """
    sid = (session_id or "").strip()
    if not SESSION_ID_RE.match(sid):
        return ""
    data = _read_json(session_cache_dir() / f"{sid}.json")
    if not isinstance(data, dict):
        return ""
    record = data.get("record") or {}
    rows = record.get("rows") if isinstance(record, dict) else {}
    if not isinstance(rows, dict):
        return ""
    outline = rows.get("turnOutline") or {}
    value = outline.get("val") if isinstance(outline, dict) else None
    turns = value.get("turns") if isinstance(value, dict) else None
    if not isinstance(turns, list):
        return ""

    picked: list[tuple[str, str]] = []
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        prompt = str(turn.get("prompt") or "").strip()
        response = str(turn.get("response") or "").strip()
        if not prompt or not response:
            continue
        picked.append((prompt, response))
        if len(picked) >= max(1, max_turns):
            break
    picked.reverse()
    if not picked:
        return ""

    chunks = []
    for prompt, response in picked:
        chunks.append(
            "用户：" + _clip(prompt, 320) + "\n"
            "助手：" + _clip(response, 620)
        )
    text = "\n\n".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def build_handoff_task(
    task: str,
    title: str,
    preset: str,
    handoff: str,
    max_chars: int = MAX_HANDOFF,
) -> str:
    """构造自动降级任务的 prompt：保留原任务，附一小段旧上下文。"""
    title = (title or "(无标题)").strip()
    preset = (preset or "unknown").strip()
    head = (
        f"【DSH 自动接续】原会话「{title}」的 agent preset「{preset}」"
        "不能直接在 headless 加载，已自动新建 Default 会话继续。"
    )
    handoff = (handoff or "").strip()
    if handoff:
        head += (
            "\n以下是原会话最近几轮摘要，仅用于接续上下文，不要原样复述：\n"
            + _clip(handoff, max_chars)
        )
    else:
        head += "\n没有取到可移植的旧上下文，请直接按当前任务继续。"
    return head + "\n\n【当前任务】\n" + (task or "").strip()


def pick_new_session(
    before_ids: set[str],
    sessions: list[dict[str, Any]],
    cwd: str | None = None,
) -> dict[str, Any] | None:
    """在跑完 headless 后，从会话列表里挑出这次新建的那个。"""
    candidates = [s for s in sessions if s.get("id") not in before_ids]
    if not candidates:
        return None
    if cwd:
        same_cwd = [s for s in candidates if s.get("cwd") == cwd]
        if same_cwd:
            candidates = same_cwd
    candidates.sort(
        key=lambda s: s.get("updated_at") or datetime.min,
        reverse=True,
    )
    return candidates[0]


def list_sessions(
    limit: int = 10,
    include_archived: bool = False,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """按最后更新时间倒序列出 DSH 对话（含工作区 cwd）。"""
    base = session_cache_dir()
    if not base.is_dir():
        return []
    archived = set() if include_archived else _archived_session_ids()
    sessions: list[dict[str, Any]] = []
    for path in base.glob("session-*.json"):
        if path.stem in archived:
            continue
        item = _parse_session(path)
        if item is None:
            continue
        if now is not None:
            try:
                item["active"] = (now - path.stat().st_mtime) < ACTIVE_WINDOW
            except OSError:
                pass
        sessions.append(item)
    sessions.sort(key=lambda s: s.get("updated_at") or datetime.min, reverse=True)
    return sessions[: max(1, limit)]


def workspace_name(cwd: str) -> str:
    """把 cwd 变成短工作区名；兼容 Windows 反斜杠路径。"""
    cwd = (cwd or "").strip()
    if not cwd:
        return "(未知工作区)"
    # Path(...).name 在 POSIX 上不会把反斜杠当分隔符；测试和远程运行时
    # 都可能收到 Windows 路径，所以先统一分隔符。
    normalized = cwd.replace("\\", "/").rstrip("/")
    name = normalized.rsplit("/", 1)[-1]
    return name or cwd


def build_command(
    exe: str | Path,
    cli: str,
    task: str,
    session_id: str | None = None,
) -> list[str]:
    args = [str(exe), "--expose-internals", str(cli), "--profile", "headless"]
    if session_id:
        args += ["--session-id", session_id]
    args.append(task)
    return args



def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，共 {len(text)} 字）"


NOISE_MARKERS = (
    "crashpad",
    "transactnamedpipe",
    "registration_protocol_win.cc",
    "devtools listening",
)


def is_noise_line(line: str) -> bool:
    """Chromium/Electron 的崩溃上报噪声，不该当成任务输出或推理。"""
    low = (line or "").lower()
    return any(marker in low for marker in NOISE_MARKERS)


def clean_stderr(text: str) -> str:
    """去掉已知噪声行，保留真正有用的 stderr。"""
    lines = [line for line in (text or "").splitlines() if line.strip() and not is_noise_line(line)]
    return "\n".join(lines).strip()


def format_result(result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"DSH 没跑起来：{result['error']}"
    if result.get("timeout"):
        return f"DSH 超过 {result.get('timeout_seconds', DEFAULT_TIMEOUT)} 秒，已终止。"
    code = int(result.get("code") or 0)
    body = (result.get("stdout") or "").strip()
    if not body:
        body = clean_stderr(result.get("stderr") or "")
        if body:
            body = "（stdout 为空，下面是诊断输出）\n" + body
        elif code != 0:
            body = "（没有可用输出；stderr 只有环境噪声）"
    body = truncate(body)
    if not body:
        body = "（DSH 没有输出）"
    prefix = "" if code == 0 else f"DSH 退出码 {code}\n"
    return prefix + body


def format_heartbeat(elapsed: float, alive: bool = True) -> str:
    """定时报告的文案；不依赖 DSH 输出（headless 的 stdout 要等结束才有）。"""
    total = max(0, int(elapsed))
    minutes, seconds = divmod(total, 60)
    state = "进程存活" if alive else "进程已退出，等输出收尾"
    return f"还在跑：已用 {minutes} 分 {seconds:02d} 秒，{state}。"



async def run_task(
    task: str,
    key: str,
    api_base: str,
    timeout: int | None = None,
    workdir: str | Path | None = None,
    session_id: str | None = None,
    heartbeat: int = 0,
    on_heartbeat: Callable[[float, bool], Awaitable[None]] | None = None,
    on_stderr: Callable[[str], Awaitable[None]] | None = None,
    stop_event: asyncio.Event | None = None,
) -> dict[str, Any]:
    """跑一次性任务/续会话。

    - 流式读取子进程 stderr（DSH 会把 agent 推理写到 stderr），通过
      ``on_stderr(line)`` 实时转发，可用于显示"思考到哪了"。
    - 传入 ``stop_event`` 可在运行中手动终止：检测到 set 时杀掉子进程并返回
      ``stopped=True``（配合 /dsh stop）。
    - ``on_heartbeat`` 保持按 ``heartbeat`` 秒报告存活。
    """
    if session_id and not SESSION_ID_RE.match(session_id):
        return {"error": f"会话 ID 格式不对：{session_id}", "code": -1}
    timeout = timeout or task_timeout()
    found = find_install()
    if not found:
        return {"error": "没找到 DeepSeek Harness 安装目录", "code": -1}
    exe, cli = found
    env = build_env(os.environ, key, api_base)
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *build_command(exe, cli, task, session_id),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(workdir or Path.home()),
        )
    except OSError as exc:
        return {"error": f"启动失败：{exc}", "code": -1}

    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []

    async def drain_stdout() -> None:
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break
            out_chunks.append(chunk)

    async def drain_stderr() -> None:
        pending = b""
        while True:
            chunk = await proc.stderr.read(8192)
            if not chunk:
                break
            err_chunks.append(chunk)
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if on_stderr:
                    try:
                        await on_stderr(line.decode("utf-8", "replace").rstrip("\r"))
                    except Exception:  # noqa: BLE001
                        pass
        if pending and on_stderr:
            try:
                await on_stderr(pending.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass

    t_out = asyncio.create_task(drain_stdout())
    t_err = asyncio.create_task(drain_stderr())

    timeout_hit = False
    stopped = False
    last_hb = 0.0
    try:
        while True:
            if proc.returncode is not None and t_out.done() and t_err.done():
                break
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                raise asyncio.TimeoutError
            if stop_event is not None and stop_event.is_set():
                stopped = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                break
            if heartbeat and on_heartbeat and (elapsed - last_hb) >= heartbeat:
                last_hb = elapsed
                try:
                    await on_heartbeat(elapsed, proc.returncode is None)
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(0.5)
    except asyncio.TimeoutError:
        timeout_hit = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        t_out.cancel()
        t_err.cancel()
        await asyncio.gather(t_out, t_err, return_exceptions=True)
        raise

    await asyncio.gather(t_out, t_err, return_exceptions=True)
    try:
        await proc.wait()
    except ProcessLookupError:
        pass
    duration = round(time.monotonic() - started, 1)
    return {
        "code": proc.returncode if proc.returncode is not None else -1,
        "stdout": b"".join(out_chunks).decode("utf-8", "replace"),
        "stderr": b"".join(err_chunks).decode("utf-8", "replace"),
        "timeout": timeout_hit,
        "stopped": stopped,
        "timeout_seconds": timeout,
        "seconds": duration,
    }


def append_audit(path: Path, record: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
