# -*- coding: utf-8 -*-
"""Telegram 增量查询核心（不依赖 AstrBot，可单独跑测试）。

职责：用本机已登录的 Telethon 会话，读取指定群自上次游标以来的新消息，
只统计条数 / 发言人 / 时间范围，并检测关键词（默认 lgd）是否发过言。
不读取、不保存、不回传任何消息正文。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

TZ = timezone(timedelta(hours=8))

# 复用本机爬虫工具的登录态（dist 目录里那份才是真正在用的会话）
DEFAULT_SOURCE_DIR = str(Path.home() / "telegram-video-crawler" / "dist")

DEFAULT_KEYWORD = "lgd"
DEFAULT_SPECIAL = "LGD的奇妙冒险"


class TgError(Exception):
    """可以直接展示给用户的错误。"""


def _fmt_span(start: datetime, end: datetime) -> str:
    s, e = start.astimezone(TZ), end.astimezone(TZ)
    if s.date() == e.date():
        return f"{s:%m-%d %H:%M}–{e:%H:%M}"
    return f"{s:%m-%d %H:%M}–{e:%m-%d %H:%M}"


def _fmt_moment(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return ""
    return f"{dt.astimezone(TZ):%m-%d %H:%M}"


class TgWatchCore:
    def __init__(
        self,
        config: dict,
        data_dir: str | Path,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = dict(config or {})
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self.session_file = self.data_dir / "tg.session"
        self.notify_path = self.data_dir / "notify.json"
        self._log = log or (lambda msg: None)
        # 常驻连接：只在第一次连一次，之后每 5 分钟巡检只发一个取消息请求
        self._client = None
        self._entity = None
        self._entity_ref = ""

    # ------------------------------------------------------------------ 配置
    def _str(self, key: str, default: str = "") -> str:
        val = self.config.get(key)
        return str(val).strip() if val is not None and str(val).strip() else default

    def _int(self, key: str, default: int) -> int:
        raw = self.config.get(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def _bool(self, key: str, default: bool) -> bool:
        raw = self.config.get(key)
        if raw is None or raw == "":
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")

    @property
    def source_dir(self) -> Path:
        return Path(self._str("source_dir", DEFAULT_SOURCE_DIR))

    @property
    def keyword(self) -> str:
        return self._str("watch_user", DEFAULT_KEYWORD)

    @property
    def special_text(self) -> str:
        return self._str("special_text", DEFAULT_SPECIAL)

    @property
    def watch_chat(self) -> str:
        return self._str("watch_chat", "")

    @property
    def max_fetch(self) -> int:
        return max(20, min(1000, self._int("max_fetch", 200)))

    # -------------------------------------------------------------- 凭据/会话
    def credentials(self) -> tuple[int, str]:
        api_id = self._int("api_id", 0)
        api_hash = self._str("api_hash", "")
        if not api_id or not api_hash:
            cfg_file = Path(self._str("source_config", "") or (self.source_dir / "config.json"))
            if cfg_file.is_file():
                try:
                    data = json.loads(cfg_file.read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    raise TgError(f"读不了爬虫配置 {cfg_file}：{exc}") from exc
                api_id = api_id or int(data.get("api_id") or 0)
                api_hash = api_hash or str(data.get("api_hash") or "").strip()
        if not api_id or not api_hash:
            raise TgError(
                "缺少 Telegram api_id / api_hash：请在插件配置里填，或把 source_config 指到爬虫的 config.json"
            )
        return api_id, api_hash

    def session_source(self) -> Path:
        raw = self._str("source_session", "")
        if raw:
            path = Path(raw)
            return path if path.suffix == ".session" else path.with_suffix(".session")
        return self.source_dir / "sessions" / "crawler.session"

    def sync_session(self) -> Path:
        """复制一份会话到插件数据目录，避免和 GUI 版抢同一个 SQLite 文件锁。"""
        src = self.session_source()
        if not src.is_file():
            raise TgError(f"找不到 Telegram 会话文件：{src}（先在爬虫工具里登录一次）")
        need_copy = True
        if self.session_file.is_file():
            try:
                need_copy = src.stat().st_mtime > self.session_file.stat().st_mtime
            except OSError:
                need_copy = True
        if need_copy:
            shutil.copy2(src, self.session_file)
            self._log(f"已同步 Telegram 会话文件 -> {self.session_file}")
        return self.session_file

    def build_proxy(self):
        """返回 python_socks 代理元组；'direct' 表示强制直连。"""
        raw = self._str("proxy", "")
        if raw.lower() in ("direct", "none", "off", "直连"):
            return None
        try:
            from python_socks import ProxyType
        except Exception:  # noqa: BLE001
            self._log("缺少 python_socks，Telegram 可能连不上（请装 python-socks）")
            return None

        if raw:
            try:
                scheme, rest = raw.split("://", 1) if "://" in raw else ("socks5", raw)
                host, port = rest.rsplit(":", 1)
                ptype = {
                    "http": ProxyType.HTTP,
                    "socks4": ProxyType.SOCKS4,
                }.get(scheme.lower(), ProxyType.SOCKS5)
                return (ptype, host, int(port))
            except Exception:  # noqa: BLE001
                self._log(f"proxy 写法解析不了（{raw}），改用系统代理")

        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            winreg.CloseKey(key)
            if enable and server and "=" not in server:
                host, port = server.rsplit(":", 1)
                return (ProxyType.SOCKS5, host, int(port))
        except Exception:  # noqa: BLE001
            return None
        return None

    # ------------------------------------------------------------------ 游标
    def load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    # ------------------------------------------------------------- 群/发送者
    @staticmethod
    def parse_ref(ref: str) -> str:
        ref = (ref or "").strip()
        matched = re.search(r"t\.me/(?:joinchat/)?([A-Za-z0-9_\-]+)", ref)
        if matched:
            return matched.group(1)
        return ref[1:] if ref.startswith("@") else ref

    async def resolve_chat(self, client, ref: str):
        parsed = self.parse_ref(ref)
        if not parsed:
            raise TgError("还没配置要监看的 Telegram 群（插件配置 watch_chat）")
        try:
            if re.fullmatch(r"-?\d+", parsed):
                return await client.get_entity(int(parsed))
            return await client.get_entity(parsed)
        except Exception as exc:  # noqa: BLE001
            target = parsed.lower()
            async for dialog in client.iter_dialogs(limit=300):
                title = (dialog.title or "").strip()
                if title.lower() == target or target in title.lower():
                    return dialog.entity
            raise TgError(
                f"找不到这个群：{ref}（可改成 @用户名、t.me 链接或数字 id）"
            ) from exc

    @staticmethod
    async def sender_text(message) -> tuple[str, str]:
        """返回 (显示名, 用户名)；都取不到就给个占位名。"""
        sender = getattr(message, "sender", None)
        if sender is None:
            try:
                sender = await message.get_sender()
            except Exception:  # noqa: BLE001
                sender = None
        if sender is None:
            uid = getattr(getattr(message, "from_id", None), "user_id", None)
            return (f"用户{uid}" if uid else "未知", "")
        if getattr(sender, "title", None):
            return str(sender.title), str(getattr(sender, "username", "") or "")
        name = " ".join(
            part
            for part in (
                getattr(sender, "first_name", "") or "",
                getattr(sender, "last_name", "") or "",
            )
            if part
        ).strip()
        username = str(getattr(sender, "username", "") or "")
        if not name:
            name = f"@{username}" if username else f"用户{getattr(sender, 'id', '?')}"
        return name, username

    # -------------------------------------------------------------- 推送目标
    def load_notify(self) -> dict:
        """自动推送的目标会话（在群里发过一次 /tg 就记下来）。"""
        try:
            data = json.loads(self.notify_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def save_notify(self, umo: str) -> None:
        umo = (umo or "").strip()
        if not umo:
            return
        old = self.load_notify().get("umo")
        if old == umo:
            return
        try:
            self.notify_path.write_text(
                json.dumps(
                    {
                        "umo": umo,
                        "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self._log(f"自动推送目标已记为 {umo}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"记录推送目标失败：{exc}")

    # ------------------------------------------------------------ 常驻连接
    async def ensure_client(self):
        """连着就直接复用；断了自己重连。省掉每次巡检的握手 + 会话初始化开销。"""
        from telethon import TelegramClient

        client = self._client
        if client is not None:
            try:
                if client.is_connected():
                    return client
            except Exception:  # noqa: BLE001
                pass
            await self.close_async()

        api_id, api_hash = self.credentials()
        session_file = self.sync_session()
        client = TelegramClient(
            str(session_file.with_suffix("")),
            api_id,
            api_hash,
            proxy=self.build_proxy(),
            connection_retries=3,
            request_retries=3,
            timeout=20,
            device_model="Desktop",
            system_version="Windows 10",
            app_version="1.0",
        )
        await client.connect()
        if not await client.is_user_authorized():
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            raise TgError("Telegram 会话失效了：先在爬虫工具里重新登录一次，再回来试 /tg")
        self._client = client
        self._entity = None
        self._entity_ref = ""
        return client

    async def close_async(self) -> None:
        client, self._client = self._client, None
        self._entity = None
        self._entity_ref = ""
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def is_connected(self) -> bool:
        try:
            return bool(self._client is not None and self._client.is_connected())
        except Exception:  # noqa: BLE001
            return False

    async def entity_cached(self, client):
        """群实体解析一次就缓存，后续巡检不再重复解析。"""
        ref = self.watch_chat
        if self._entity is not None and self._entity_ref == ref:
            return self._entity
        entity = await self.resolve_chat(client, ref)
        self._entity, self._entity_ref = entity, ref
        return entity

    # ------------------------------------------------------------------ 主流程
    async def check_async(self) -> dict:
        from telethon.errors import FloodWaitError

        started = time.time()
        try:
            client = await self.ensure_client()
            entity = await self.entity_cached(client)
            title = (
                getattr(entity, "title", None)
                or getattr(entity, "username", None)
                or self.watch_chat
            )
            chat_id = int(getattr(entity, "id", 0) or 0)

            state = self.load_state()
            same_chat = int(state.get("chat_id") or 0) == chat_id
            last_id = int(state.get("last_id") or 0) if same_chat else 0

            if not last_id:
                latest = await client.get_messages(entity, limit=1)
                latest_id = int(latest[0].id) if latest else 0
                now_iso = datetime.now(TZ).isoformat(timespec="seconds")
                self.save_state(
                    {
                        "chat_id": chat_id,
                        "chat_title": str(title),
                        "last_id": latest_id,
                        "last_time": (
                            getattr(latest[0], "date", None).isoformat() if latest else ""
                        ),
                        "checked_at": now_iso,
                    }
                )
                return {
                    "first_run": True,
                    "chat_title": str(title),
                    "chat_id": chat_id,
                    "latest_id": latest_id,
                    "elapsed": round(time.time() - started, 2),
                }

            # reverse=True：从游标往后按时间正序取，超出上限时游标只推进到最后一条，
            # 剩下的下次继续取，不会跳过消息。
            try:
                messages = await client.get_messages(
                    entity, min_id=last_id, limit=self.max_fetch, reverse=True
                )
            except FloodWaitError as exc:
                raise TgError(f"Telegram 限流了，{int(exc.seconds) + 1} 秒后再试") from exc
            total = len(messages)
            keyword = self.keyword
            lowered = keyword.lower()
            counts: dict[str, int] = {}
            hits: list[str] = []
            hit_count = 0
            fwd_count = 0
            for msg in messages:
                if getattr(msg, "fwd_from", None):
                    fwd_count += 1
                name, username = await self.sender_text(msg)
                haystack = f"{name} {username}".lower()
                # 命中关键词的人不进出「发言人」列表，只用标签体现，避免出现「lgd 41」
                if lowered and lowered in haystack:
                    hit_count += 1
                    if name not in hits:
                        hits.append(name)
                else:
                    counts[name] = counts.get(name, 0) + 1

            newest_id = int(messages[-1].id) if messages else last_id
            newest_time = (
                messages[-1].date.isoformat()
                if messages and getattr(messages[-1], "date", None)
                else str(state.get("last_time") or "")
            )
            now_iso = datetime.now(TZ).isoformat(timespec="seconds")
            self.save_state(
                {
                    "chat_id": chat_id,
                    "chat_title": str(title),
                    "last_id": newest_id,
                    "last_time": newest_time,
                    "checked_at": now_iso,
                }
            )

            span = ""
            if messages:
                span = _fmt_span(messages[0].date, messages[-1].date)
            return {
                "first_run": False,
                "chat_title": str(title),
                "chat_id": chat_id,
                "total": total,
                "more": total >= self.max_fetch,
                "senders": counts,
                "hits": hits,
                "hit_count": hit_count,
                "forwards": fwd_count,
                "keyword": keyword,
                "special": self.special_text,
                "show_senders": self._bool("show_senders", True),
                "span": span,
                "before_id": last_id,
                "checked_at": _fmt_moment(state.get("checked_at")),
                "elapsed": round(time.time() - started, 2),
            }
        except TgError:
            raise
        except FloodWaitError as exc:
            raise TgError(f"Telegram 限流了，{int(exc.seconds) + 1} 秒后再试") from exc
        except Exception:
            await self.close_async()  # 连接可能已经坏了，下一轮重建
            raise

    def check_blocking(self) -> dict:
        timeout = max(20, min(300, self._int("timeout_seconds", 90)))
        return asyncio.run(asyncio.wait_for(self.check_async(), timeout=timeout))

    # ------------------------------------------------------------------ 文案
    def format_result(self, result: dict) -> str:
        title = result.get("chat_title") or "Telegram 群"
        if result.get("first_run"):
            return (
                f"📡 已接入监看：{title}\n"
                f"基线记为最新一条（#{result.get('latest_id')}），"
                f"之后的新消息从下次 /tg 开始统计。"
            )
        total = int(result.get("total") or 0)
        if total <= 0:
            checked = result.get("checked_at")
            tail = f"（上次检查 {checked}）" if checked else ""
            return f"📡 {title}：暂无新消息{tail}，仍停在 #{result.get('before_id')}。"

        lines = [
            f"📡 {title}：新增 {total}{'+' if result.get('more') else ''} 条"
            f"（{result.get('span') or '时间未知'}）"
        ]
        counts = result.get("senders") or {}
        if self._bool("show_senders", True) and counts:
            top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
            more = "…" if len(counts) > 4 else ""
            lines.append("👥 " + "、".join(f"{name}×{num}" for name, num in top) + more)
        fwd = int(result.get("forwards") or 0)
        if fwd > 0:
            lines.append(f"↗ 其中转发 {fwd} 条")
        if result.get("hits"):
            lines.append(f"⚡ {result.get('special')}")
        return "\n".join(lines)


class TgWorker:
    """把 Telethon 客户端固定在一个后台线程的独立事件循环里。

    好处：连接常驻复用，每 5 分钟巡检只发一次 get_messages，不再每次重连、
    重新解析群、重新握手；同时不占用 AstrBot 主事件循环。
    """

    def __init__(self, core: TgWatchCore) -> None:
        self.core = core
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._tasks = 0

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="tg-watch", daemon=True)
        self._thread.start()
        return self._ready.wait(15)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self.core.close_async())
            except Exception:  # noqa: BLE001
                pass
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass
            self._loop = None

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:  # noqa: BLE001
            pass
        thread = self._thread
        if thread is not None:
            thread.join(6)
        self._thread = None

    # -------------------------------------------------------------------- 调用
    def call(self, timeout: float = 90.0) -> dict:
        if not self.start() or self._loop is None:
            raise RuntimeError("TG 后台线程启动失败")
        self._tasks += 1
        try:
            future = asyncio.run_coroutine_threadsafe(self.core.check_async(), self._loop)
            return future.result(timeout)
        finally:
            self._tasks -= 1

    @property
    def alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    @property
    def connected(self) -> bool:
        return self.core.is_connected()
