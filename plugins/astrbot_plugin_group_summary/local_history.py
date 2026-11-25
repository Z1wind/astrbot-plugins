# -*- coding: utf-8 -*-
"""只读读取本机 NTQQ 群消息，作为群总结的补充消息源。

这里不直接解析 SQLCipher：复用本机已有的 `@powercess/qq-mcp`（Node）来解密
`nt_msg.db` 并执行只读查询。插件只拿到指定 QQ 群的最近消息，不保存原始聊天
记录；找不到 key / qq-mcp / Node 时静默回退到 AstrBot 自己的群消息缓存。

安全边界：
- 只查配置里的一个群（默认使用配置中的群号），不会查私聊或其他群；
- 不把消息写文件，不把 passphrase 写日志；
- 查询失败、超时、QQ 未登录都不影响总结。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_GROUP_QQ = 0
DEFAULT_GROUP_OPENID = ""
DEFAULT_KEY_FILE = Path.home() / ".qq-mcp" / "qqkey"

SUMMARY_KEYWORDS = ("总结", "群总结", "趣事", "好玩的事", "乐子")
IGNORE_NAMES = {"机器人"}
# 本机 bot 自己的号；本地历史会包含它自己的发言，
# 不剔除会被总结当成“群友原话”画进图里，也会虚增参与人数。
IGNORE_QIDS = set()
QZONE_AVATAR = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"

_ROSTER_CACHE: dict[int, tuple[float, dict[int, dict[str, Any]]]] = {}
_ROSTER_TTL = 600.0


class LocalHistoryError(RuntimeError):
    """本地消息源不可用；调用方应回退到 bot 缓存。"""


def _candidate_mcp_paths() -> list[Path]:
    """查找本机 qq-mcp 的 mcp_call.mjs（Doubao 沙箱 / 全局 npm）。"""
    candidates: list[Path] = []
    local_app = os.environ.get("LOCALAPPDATA") or ""
    if local_app:
        bases = Path(local_app) / "Doubao" / "User Data" / "sandbox_runtime" / "bases"
        if bases.is_dir():
            try:
                candidates.extend(bases.glob("*/node/node_modules/@powercess/qq-mcp/mcp_call.mjs"))
            except OSError:
                pass
    # 少数机器会把 qq-mcp 放到用户目录；找不到时仍可用配置覆盖。
    home = Path.home()
    for pattern in (
        ".qq-mcp/mcp_call.mjs",
        ".npm-global/node_modules/@powercess/qq-mcp/mcp_call.mjs",
        "AppData/Roaming/npm/node_modules/@powercess/qq-mcp/mcp_call.mjs",
    ):
        p = home / pattern
        if p.is_file():
            candidates.append(p)
    # 去重，最新的优先
    unique = {str(p): p for p in candidates if p.is_file()}
    return sorted(unique.values(), key=lambda p: p.stat().st_mtime, reverse=True)


def discover_runtime(
    mcp_path: str = "", node_path: str = ""
) -> tuple[Path, Path]:
    """返回 (node.exe, mcp_call.mjs)；缺失时抛 LocalHistoryError。"""
    mcp: Path | None = None
    if mcp_path:
        p = Path(mcp_path).expanduser()
        if p.is_dir():
            p = p / "mcp_call.mjs"
        if p.is_file():
            mcp = p
    if mcp is None:
        found = _candidate_mcp_paths()
        if found:
            mcp = found[0]
    if mcp is None:
        raise LocalHistoryError("没找到 qq-mcp/mcp_call.mjs")

    node: Path | None = None
    if node_path:
        p = Path(node_path).expanduser()
        if p.is_file():
            node = p
    if node is None:
        # .../node/node_modules/@powercess/qq-mcp/mcp_call.mjs
        sibling = mcp.parents[3] / "node.exe" if len(mcp.parents) > 3 else None
        if sibling and sibling.is_file():
            node = sibling
    if node is None:
        which = shutil.which("node")
        if which:
            node = Path(which)
    if node is None:
        raise LocalHistoryError("没找到 node.exe")
    return node, mcp


def read_passphrase(key_file: str = "") -> str:
    """读取 qq-mcp 保存的 16 位 passphrase；只用于子进程环境变量。"""
    p = Path(key_file).expanduser() if key_file else DEFAULT_KEY_FILE
    if p.is_file():
        try:
            value = p.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise LocalHistoryError(f"读取 qqkey 失败: {exc}") from exc
        if value:
            return value
    env = (os.environ.get("QQ_MCP_PASSPHRASE") or "").strip()
    if env:
        return env
    raise LocalHistoryError("没有 qqkey；请先运行 qq-mcp capture_offline_key")


async def call_tool(
    tool: str,
    args: dict[str, Any],
    *,
    mcp_path: str = "",
    node_path: str = "",
    key_file: str = "",
    timeout: float = 120.0,
) -> dict[str, Any]:
    """调用 qq-mcp MCP 工具，返回解析后的 JSON。"""
    node, mcp = discover_runtime(mcp_path, node_path)
    passphrase = read_passphrase(key_file)
    encoded = base64.b64encode(
        json.dumps(args, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    # 每次查询用独立临时 QQ_MCP_HOME：qq-mcp 解密出的完整 SQLite 快照只存在这里，
    # 查询结束立刻删除，不在 ~/.qq-mcp/plain 留一份包含私聊的明文库。
    temp_home = Path(tempfile.mkdtemp(prefix="astrbot_qq_mcp_"))
    env = os.environ.copy()
    env["QQ_MCP_PASSPHRASE"] = passphrase
    env["QQ_MCP_HOME"] = str(temp_home)
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                str(node),
                str(mcp),
                tool,
                encoded,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise LocalHistoryError(f"启动 qq-mcp 失败: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise LocalHistoryError("qq-mcp 查询超时") from exc
        except asyncio.CancelledError:
            # 外层 _with_local_history 的 wait_for(10s) 取消时，别把子进程留成孤儿。
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise

        out = stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace")
        if "result:" not in out:
            detail = (err or out).strip().splitlines()
            tail = detail[-1][:200] if detail else f"exit={proc.returncode}"
            raise LocalHistoryError(f"qq-mcp 没返回结果: {tail}")
        payload = out.split("result:", 1)[1].strip()
        try:
            data, _ = json.JSONDecoder().raw_decode(payload)
        except json.JSONDecodeError as exc:
            raise LocalHistoryError(f"qq-mcp 返回不是 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LocalHistoryError("qq-mcp 返回格式不对")
        return data
    finally:
        shutil.rmtree(temp_home, ignore_errors=True)


def _clean_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    # 本地库有时把 At 宏带在正文里，清掉避免图里出现一长串 openid。
    text = re.sub(r"<@!?[^>]+>", "", text)
    text = re.sub(r"@\S+\s*", "", text)
    # qq-mcp 对图片会给 “[图片] <hash>.png”，图里只保留 [图片]。
    text = re.sub(
        r"\[图片\]\s+[0-9a-fA-F]{16,}\.[A-Za-z0-9]+", "[图片]", text
    )
    return re.sub(r"\s+", " ", text).strip()


def _is_command(text: str) -> bool:
    clean = re.sub(r"\s+", "", text)
    # “消息总结 / 最近有啥趣事 / 总结一下”都算触发语；正常长句里的“总结”不误杀。
    return len(clean) <= 12 and any(k in clean for k in SUMMARY_KEYWORDS)


def _name_for(qq: int, roster: dict[int, dict[str, Any]], raw_name: Any) -> str:
    info = roster.get(qq) or {}
    name = str(info.get("name") or raw_name or "").strip()
    if not name:
        name = f"群友{qq % 10000:04d}" if qq > 0 else "群友"
    return name


def parse_messages(
    data: dict[str, Any],
    roster: dict[int, dict[str, Any]],
    since: float,
    until: float,
) -> list[dict[str, Any]]:
    """把 qq-mcp 的 group 消息转成插件内部消息格式。"""
    result: list[dict[str, Any]] = []
    for raw in data.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        try:
            ts = int(raw.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts < int(since) - 2 or ts > int(until) + 2:
            continue
        try:
            qq = int(raw.get("senderQq") or 0)
        except (TypeError, ValueError):
            qq = 0
        # qq<=0 多为机器人/系统/戳一戳；不拿来做“群友发言”
        if qq <= 0:
            continue
        text = _clean_text(raw.get("text"))
        if not text or _is_command(text):
            continue
        if qq in IGNORE_QIDS:
            continue
        name = _name_for(qq, roster, raw.get("senderName"))
        if name in IGNORE_NAMES:
            continue
        result.append(
            {
                "uid": f"qq:{qq}",
                "name": name,
                "text": text[:300],
                "ts": ts,
                "avatar": QZONE_AVATAR.format(qq=qq),
            }
        )
    result.sort(key=lambda m: (int(m["ts"]), str(m.get("uid"))))
    return result


def merge_messages(
    buffered: list[dict[str, Any]],
    local: list[dict[str, Any]],
    limit: int = 800,
) -> list[dict[str, Any]]:
    """本地消息优先，并补上本地快照之后才到的最新 bot 消息。"""
    if not local:
        return buffered

    def key(m: dict[str, Any]) -> tuple[int, str]:
        try:
            ts = int(float(m.get("ts") or 0))
        except (TypeError, ValueError):
            ts = 0
        return ts, str(m.get("text") or "").strip()

    merged = list(local)
    seen = {key(m) for m in merged}
    latest_local = max((key(m)[0] for m in merged), default=0)
    # 同一个人本地用 qq:<QQ>，bot 用 openid；按昵称把补进来的最新消息归一到本地 uid，
    # 避免参与人数把同一个人算两次。
    name_to_uid = {
        str(m.get("name") or ""): str(m.get("uid") or "")
        for m in local
        if str(m.get("name") or "").strip()
    }
    for m in buffered:
        k = key(m)
        if k in seen:
            continue
        # 本地快照可能落后几秒，补上比本地最新一条更新的 bot 消息
        if k[0] > latest_local:
            item = dict(m)
            mapped = name_to_uid.get(str(item.get("name") or "").strip())
            if mapped:
                item["uid"] = mapped
            merged.append(item)
            seen.add(k)
    merged.sort(key=lambda m: (int(float(m.get("ts") or 0)), str(m.get("uid"))))
    if limit > 0 and len(merged) > limit:
        merged = merged[-limit:]
    return merged


async def _fetch_roster(
    group_qq: int,
    *,
    mcp_path: str,
    node_path: str,
    key_file: str,
) -> dict[int, dict[str, Any]]:
    cached = _ROSTER_CACHE.get(group_qq)
    if cached and time.time() - cached[0] < _ROSTER_TTL:
        return cached[1]
    data = await call_tool(
        "list_group_members",
        {"groupQq": group_qq, "limit": 5000},
        mcp_path=mcp_path,
        node_path=node_path,
        key_file=key_file,
        timeout=60.0,
    )
    roster: dict[int, dict[str, Any]] = {}
    for item in data.get("members") or []:
        if not isinstance(item, dict):
            continue
        try:
            qq = int(item.get("qq") or 0)
        except (TypeError, ValueError):
            continue
        if qq <= 0:
            continue
        # 优先昵称（与 AstrBot 的 username 一致），没有再用群名片
        name = str(item.get("nickname") or item.get("card") or "").strip()
        if name:
            roster[qq] = {"name": name, "uid": item.get("uid") or ""}
    _ROSTER_CACHE[group_qq] = (time.time(), roster)
    return roster


async def load_group_history(
    group_qq: int = DEFAULT_GROUP_QQ,
    *,
    window_seconds: float = 7200.0,
    limit: int = 800,
    mcp_path: str = "",
    node_path: str = "",
    key_file: str = "",
) -> list[dict[str, Any]]:
    """只读查询本机 QQ 群里最近 window_seconds 的消息；失败抛 LocalHistoryError。"""
    if group_qq <= 0:
        raise LocalHistoryError("local_qq_group_qq 未配置")
    now = time.time()
    data = await call_tool(
        "query_chat_messages",
        {
            "chat": "group",
            "groupQq": group_qq,
            "since": int(now - window_seconds),
            "until": int(now),
            "limit": max(1, min(5000, int(limit))),
        },
        mcp_path=mcp_path,
        node_path=node_path,
        key_file=key_file,
    )
    messages = data.get("messages") or []
    if not messages:
        return []
    try:
        roster = await _fetch_roster(
            group_qq,
            mcp_path=mcp_path,
            node_path=node_path,
            key_file=key_file,
        )
    except LocalHistoryError:
        roster = {}
    return parse_messages(data, roster, now - window_seconds, now)
