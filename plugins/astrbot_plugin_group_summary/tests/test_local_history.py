# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_history import merge_messages, parse_messages  # noqa: E402


class LocalHistoryTests(unittest.TestCase):
    def test_parse_maps_roster_and_filters_command(self):
        data = {
            "messages": [
                {
                    "timestamp": 100,
                    "senderQq": 123,
                    "senderName": "",
                    "text": "<@!123> 原话一句",
                },
                {
                    "timestamp": 101,
                    "senderQq": 456,
                    "senderName": "用户C",
                    "text": "@机器人 消息总结",
                },
                {
                    "timestamp": 102,
                    "senderQq": 0,
                    "senderName": "机器人",
                    "text": "🔥 总结好了",
                },
                {
                    "timestamp": 103,
                    "senderQq": 789,
                    "senderName": "用户B",
                    "text": "原话两句",
                },
            ]
        }
        roster = {123: {"name": "用户A"}}
        msgs = parse_messages(data, roster, since=0, until=1000)
        self.assertEqual([m["name"] for m in msgs], ["用户A", "用户B"])
        self.assertEqual(msgs[0]["uid"], "qq:123")
        self.assertEqual(msgs[0]["text"], "原话一句")
        self.assertIn("nk=123", msgs[0]["avatar"])

    def test_parse_filters_bot_self(self):
        data = {
            "messages": [
                {
                    "timestamp": 150,
                    "senderQq": 999999999,
                    "senderName": "机器人",
                    "text": "我自己的回复",
                },
                {
                    "timestamp": 151,
                    "senderQq": 123,
                    "senderName": "用户A",
                    "text": "群友原话",
                },
            ]
        }
        msgs = parse_messages(data, {}, since=0, until=1000)
        self.assertEqual([m["text"] for m in msgs], ["群友原话"])

    def test_parse_collapses_local_image_hash(self):
        data = {
            "messages": [
                {
                    "timestamp": 200,
                    "senderQq": 999,
                    "senderName": "用户B",
                    "text": "[图片] 497688c243b72813f383200e5b677e0f.png",
                }
            ]
        }
        msgs = parse_messages(data, {}, since=0, until=1000)
        self.assertEqual(msgs[0]["text"], "[图片]")

    def test_merge_prefers_local_and_keeps_newer_bot_message(self):
        local = [
            {"uid": "qq:1", "name": "用户A", "text": "同一句", "ts": 100, "avatar": ""},
            {"uid": "qq:2", "name": "用户C", "text": "本地后一句", "ts": 110, "avatar": ""},
        ]
        buffered = [
            {"uid": "openid-a", "name": "用户A", "text": "同一句", "ts": 100, "avatar": ""},
            {"uid": "openid-b", "name": "用户C", "text": "本地后一句", "ts": 110, "avatar": ""},
            {"uid": "openid-c", "name": "用户B", "text": "刚发的", "ts": 120, "avatar": ""},
            {"uid": "openid-d", "name": "用户B", "text": "更早的", "ts": 50, "avatar": ""},
        ]
        merged = merge_messages(buffered, local, limit=10)
        self.assertEqual([m["text"] for m in merged], ["同一句", "本地后一句", "刚发的"])
        self.assertEqual(merged[0]["uid"], "qq:1")
        self.assertNotIn("更早的", [m["text"] for m in merged])

    def test_merge_maps_newer_buffer_uid_by_name(self):
        local = [{"uid": "qq:3", "name": "用户B", "text": "本地", "ts": 100}]
        buffered = [{"uid": "openid-x", "name": "用户B", "text": "最新", "ts": 110}]
        merged = merge_messages(buffered, local, limit=10)
        self.assertEqual(merged[-1]["text"], "最新")
        self.assertEqual(merged[-1]["uid"], "qq:3")

    def test_merge_without_local_returns_buffer(self):
        buffered = [{"uid": "a", "name": "用户A", "text": "x", "ts": 1}]
        self.assertIs(merge_messages(buffered, [], limit=10), buffered)


if __name__ == "__main__":
    unittest.main()
