# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsh_runner import (  # noqa: E402
    build_command,
    build_env,
    clean_stderr,
    format_heartbeat,
    format_result,
    heartbeat_interval,
    is_noise_line,
    list_sessions,
    reasoning_interval,
    reasoning_max_reports,
    select_provider,
    session_info,
    task_timeout,
    truncate,
    workspace_name,
)


def _write_session(base: Path, sid: str, title: str, cwd: str, mtime: float) -> None:
    payload = {
        "record": {
            "identity": {"createdAt": 1700000000000, "cwd": cwd},
            "rows": {"title": {"val": title}},
        }
    }
    path = base / f"{sid}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (mtime, mtime))


class RunnerTests(unittest.TestCase):
    def test_select_provider_prefers_enabled(self):
        cfg = {
            "provider": [
                {"id": "a/x", "enable": False, "provider_source_id": "a", "model": "x"},
                {"id": "b/y", "enable": True, "provider_source_id": "b", "model": "y"},
            ],
            "provider_sources": [
                {"id": "a", "key": "ka", "api_base": "https://a/v1"},
                {"id": "b", "key": "kb", "api_base": "https://b/v1/"},
            ],
        }
        key, base, label = select_provider(cfg)
        self.assertEqual(key, "kb")
        self.assertEqual(base, "https://b/v1")
        self.assertEqual(label, "b/y")

    def test_select_provider_falls_back_to_keyed_source(self):
        cfg = {
            "provider": [],
            "provider_sources": [
                {"id": "a", "key": ""},
                {"id": "b", "api_key": "kb"},
            ],
        }
        self.assertEqual(select_provider(cfg)[0], "kb")

    def test_select_provider_handles_key_list(self):
        cfg = {
            "provider": [{"id": "b/y", "enable": True, "provider_source_id": "b", "model": "y"}],
            "provider_sources": [{"id": "b", "key": ["test-key-1", "test-key-2"], "api_base": "https://b/v1"}],
        }
        self.assertEqual(select_provider(cfg), ("test-key-1", "https://b/v1", "b/y"))

    def test_select_provider_skips_enabled_embedding_without_key(self):
        cfg = {
            "provider": [
                {
                    "id": "ollama_embedding/bge-m3",
                    "enable": True,
                    "provider_source_id": "ollama_embedding",
                    "model": "bge-m3",
                },
                {
                    "id": "chat/deepseek",
                    "enable": True,
                    "provider_source_id": "chat",
                    "model": "deepseek",
                },
            ],
            "provider_sources": [
                {
                    "id": "ollama_embedding",
                    "type": "ollama_embedding",
                    "provider_type": "embedding",
                },
                {
                    "id": "chat",
                    "type": "openai_chat_completion",
                    "provider_type": "chat_completion",
                    "key": ["test-chat"],
                    "api_base": "https://chat/v1/",
                },
            ],
        }
        self.assertEqual(
            select_provider(cfg),
            ("test-chat", "https://chat/v1", "chat/deepseek"),
        )

    def test_select_provider_never_uses_embedding_even_with_key(self):
        cfg = {
            "provider": [
                {
                    "id": "emb/x",
                    "enable": True,
                    "provider_source_id": "emb",
                    "model": "x",
                },
                {
                    "id": "chat/y",
                    "enable": True,
                    "provider_source_id": "chat",
                    "model": "y",
                },
            ],
            "provider_sources": [
                {
                    "id": "emb",
                    "type": "openai_embedding",
                    "provider_type": "embedding",
                    "key": "test-embedding",
                },
                {
                    "id": "chat",
                    "type": "openai_chat_completion",
                    "provider_type": "chat_completion",
                    "key": "test-chat",
                },
            ],
        }
        self.assertEqual(select_provider(cfg)[0], "test-chat")

    def test_select_provider_fallback_skips_embedding(self):
        cfg = {
            "provider": [],
            "provider_sources": [
                {
                    "id": "emb",
                    "type": "openai_embedding",
                    "provider_type": "embedding",
                    "key": "test-embedding",
                },
                {
                    "id": "chat",
                    "type": "openai_chat_completion",
                    "provider_type": "chat_completion",
                    "key": "test-chat",
                    "api_base": "https://chat/v1",
                },
            ],
        }
        self.assertEqual(select_provider(cfg), ("test-chat", "https://chat/v1", "chat"))

    def test_build_env_keeps_base_and_sets_dsh(self):
        env = build_env({"PATH": "x"}, "k", "https://b/v1")
        self.assertEqual(env["PATH"], "x")
        self.assertEqual(env["ELECTRON_RUN_AS_NODE"], "1")
        self.assertEqual(env["DEEPSEEK_API_KEY"], "k")
        self.assertEqual(env["QIANYE_API_KEY"], "k")
        self.assertEqual(env["DEEPSEEK_BASE_URL"], "https://b/v1")

    def test_truncate_marks_cut(self):
        text = truncate("a" * 50, 10)
        self.assertTrue(text.startswith("a" * 10))
        self.assertIn("已截断", text)
        self.assertNotIn("已截断", truncate("short", 10))

    def test_format_result_timeout_and_error(self):
        self.assertIn("超过 5 秒", format_result({"timeout": True, "timeout_seconds": 5}))
        self.assertIn("没跑起来", format_result({"error": "boom"}))
        self.assertIn("退出码 2", format_result({"code": 2, "stdout": "bad"}))
        self.assertEqual(format_result({"code": 0, "stdout": "ok"}), "ok")

    def test_build_command_new_and_resume(self):
        new = build_command("exe.exe", "cli.js", "hi")
        self.assertEqual(new[-1], "hi")
        self.assertNotIn("--session-id", new)

        sid = "session-00000000-0000-0000-0000-000000000001"
        resumed = build_command("exe.exe", "cli.js", "继续", sid)
        self.assertEqual(resumed[-3:], ["--session-id", sid, "继续"])

    def test_list_sessions_parses_and_sorts(self):
        sid1 = "session-00000000-0000-0000-0000-000000000001"
        sid2 = "session-00000000-0000-0000-0000-000000000002"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_session(base, sid1, "旧对话", r"C:\work\a", 1000)
            _write_session(base, sid2, "新对话", r"C:\work\b", 2000)
            with mock.patch("dsh_runner.session_cache_dir", return_value=base), \
                    mock.patch("dsh_runner._archived_session_ids", return_value=set()):
                items = list_sessions(limit=5, now=10_000)
        self.assertEqual([x["id"] for x in items], [sid2, sid1])
        self.assertEqual(items[0]["title"], "新对话")
        self.assertEqual(items[0]["cwd"], r"C:\work\b")
        self.assertFalse(items[0]["active"])

    def test_session_info_rejects_bad_id(self):
        self.assertIsNone(session_info("../evil"))
        self.assertIsNone(session_info("session-not-a-real-id"))

    def test_format_heartbeat(self):
        text = format_heartbeat(125)
        self.assertIn("2 分 05 秒", text)
        self.assertIn("进程存活", text)
        self.assertIn("已退出", format_heartbeat(10, False))

    def test_timeout_and_heartbeat_env(self):
        with mock.patch.dict(
            os.environ,
            {"DSH_BRIDGE_TIMEOUT": "90", "DSH_BRIDGE_HEARTBEAT": "15"},
        ):
            self.assertEqual(task_timeout(), 90)
            self.assertEqual(heartbeat_interval(), 15)
        with mock.patch.dict(
            os.environ,
            {"DSH_BRIDGE_TIMEOUT": "bad", "DSH_BRIDGE_HEARTBEAT": "0"},
        ):
            self.assertEqual(task_timeout(), 3600)
            self.assertEqual(heartbeat_interval(), 0)

    def test_reasoning_env_and_noise_filter(self):
        with mock.patch.dict(
            os.environ,
            {"DSH_BRIDGE_REASONING_INTERVAL": "30", "DSH_BRIDGE_REASONING_MAX": "2"},
        ):
            self.assertEqual(reasoning_interval(), 30)
            self.assertEqual(reasoning_max_reports(), 2)
        with mock.patch.dict(
            os.environ,
            {"DSH_BRIDGE_REASONING_INTERVAL": "bad", "DSH_BRIDGE_REASONING_MAX": "0"},
        ):
            self.assertEqual(reasoning_interval(), 120)
            self.assertEqual(reasoning_max_reports(), 0)
        noise = "[1028/162032.047:ERROR:third_party\\crashpad\\util\\win\\registration_protocol_win.cc:136] TransactNamedPipe: 有更多数据可用。 (0xEA)"
        self.assertTrue(is_noise_line(noise))
        self.assertFalse(is_noise_line("The user asks to open UU remote."))
        self.assertEqual(clean_stderr(noise + "\nreal error\n"), "real error")

    def test_format_result_hides_crash_noise(self):
        text = format_result(
            {
                "code": 1,
                "stdout": "",
                "stderr": "[1028/162032.047:ERROR:third_party\\crashpad\\x.cc] TransactNamedPipe: 0xEA",
            }
        )
        self.assertIn("退出码 1", text)
        self.assertIn("环境噪声", text)
        self.assertNotIn("TransactNamedPipe", text)

    def test_workspace_name(self):
        self.assertEqual(workspace_name(r"C:\work\demo"), "demo")
        self.assertEqual(workspace_name(""), "(未知工作区)")

    def test_append_audit(self):
        from dsh_runner import append_audit

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "audit.jsonl"
            append_audit(p, {"task": "hi", "code": 0})
            row = json.loads(p.read_text(encoding="utf-8").strip())
            self.assertEqual(row["task"], "hi")


if __name__ == "__main__":
    unittest.main()
