# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from analysis import (  # noqa: E402
    build_transcript,
    fallback_story,
    find_hotspots,
    normalize_selection,
)
from render import render_summary  # noqa: E402


def _m(uid, name, text, ts):
    return {"uid": uid, "name": name, "text": text, "ts": ts, "avatar": ""}


class AnalysisTests(unittest.TestCase):
    def test_find_hotspots_prefers_more_participants(self):
        msgs = [
            _m("a", "阿伟", "我抢到券了哈哈哈哈", 1000),
            _m("b", "小羊", "链接呢？", 1010),
            _m("c", "阿杰", "我也要", 1020),
            _m("d", "小林", "手慢无", 1030),
            _m("a", "阿伟", "没了", 2000),
        ]
        spots = find_hotspots(msgs, window_seconds=60, step_seconds=30, limit=2)
        self.assertTrue(spots)
        self.assertEqual(spots[0].participants, 4)
        self.assertEqual(spots[0].heat, 4)
        self.assertGreater(spots[0].score, 0)

    def test_find_hotspots_dedupes_overlapping_windows(self):
        msgs = [_m(f"u{i}", f"人{i}", "哈哈哈哈", 1000 + i) for i in range(8)]
        spots = find_hotspots(msgs, window_seconds=60, step_seconds=10, limit=6)
        self.assertEqual(len(spots), 1)
        self.assertEqual(spots[0].participants, 8)

    def test_find_hotspots_keeps_separated_windows(self):
        msgs = [
            _m("a", "阿伟", "第一段", 1000),
            _m("b", "小羊", "第一段", 1010),
            _m("c", "阿杰", "第一段", 1020),
            _m("d", "小林", "第二段", 1600),
            _m("e", "阿杰", "第二段", 1610),
            _m("f", "阿伟", "第二段", 1620),
        ]
        spots = find_hotspots(msgs, window_seconds=60, step_seconds=30, limit=6)
        self.assertEqual(len(spots), 2)
        self.assertTrue(all(s.participants == 3 for s in spots))

    def test_fallback_story_uses_real_messages(self):
        msgs = [_m("a", "阿伟", "原话一句", 1000), _m("b", "小羊", "原话两句", 1010)]
        spots = find_hotspots(msgs, window_seconds=60)
        story = fallback_story(spots, "测试群")
        self.assertEqual(story["group"], "测试群")
        self.assertEqual(story["events"][0]["heat"], 2)
        self.assertEqual(story["events"][0]["dialogues"][0]["text"], "原话一句")

    def test_transcript_ids_and_selection_use_exact_source_text(self):
        msgs = [
            _m("a", "阿伟", "我抢到券了哈哈哈哈 [视频]", 1000),
            _m("b", "小羊", "链接呢？", 1010),
            _m("c", "阿杰", "我也要", 1020),
            _m("d", "小林", "手慢无", 2000),
        ]
        source, transcript = build_transcript(msgs, max_messages=4)
        self.assertEqual(len(source), 4)
        self.assertIn("m001 ", transcript)
        self.assertIn("阿伟: 我抢到券了哈哈哈哈", transcript)
        self.assertNotIn("[视频]", transcript)
        story = {
            "title": "测试群今日趣事",
            "events": [
                {
                    "topic": "抢券翻车",
                    "desc": "阿伟抢到券，小羊追问链接。",
                    "ids": ["m001", "m002", "m003"],
                }
            ],
        }
        normalized = normalize_selection(story, source, "测试群")
        self.assertIsNotNone(normalized)
        event = normalized["events"][0]
        self.assertEqual(event["dialogues"][0]["text"], "我抢到券了哈哈哈哈")
        self.assertEqual(event["participants"], 3)
        self.assertEqual(event["heat"], 3)

    def test_selection_rejects_single_speaker(self):
        msgs = [
            _m("a", "阿伟", "第一句", 1000),
            _m("a", "阿伟", "第二句", 1010),
            _m("a", "阿伟", "第三句", 1020),
        ]
        source, _ = build_transcript(msgs, max_messages=3)
        story = {
            "events": [
                {
                    "topic": "刷屏",
                    "desc": "阿伟连续说了三句。",
                    "ids": ["m001", "m002", "m003"],
                }
            ]
        }
        self.assertIsNone(normalize_selection(story, source, "测试群"))

    def test_selection_rejects_unknown_ids_and_media_only(self):
        msgs = [
            _m("a", "阿伟", "[视频]", 1000),
            _m("b", "小羊", "原话一句", 1010),
            _m("c", "阿杰", "原话两句", 1020),
        ]
        source, _ = build_transcript(msgs, max_messages=3)
        story = {
            "events": [
                {
                    "topic": "测试",
                    "desc": "测试",
                    "ids": ["m001", "m002", "m003", "m099"],
                }
            ]
        }
        self.assertIsNone(normalize_selection(story, source, "测试群"))

    def test_selection_replaces_hallucinated_desc(self):
        msgs = [
            _m("a", "阿伟", "我抢到券了", 1000),
            _m("b", "小羊", "链接呢？", 1010),
            _m("c", "阿杰", "我也要", 1020),
        ]
        source, _ = build_transcript(msgs, max_messages=3)
        story = {
            "events": [
                {
                    "topic": "抢券",
                    "desc": "阿伟说“不存在的话”，还提到小林。",
                    "ids": ["m001", "m002", "m003"],
                }
            ]
        }
        normalized = normalize_selection(story, source, "测试群")
        self.assertIsNotNone(normalized)
        desc = normalized["events"][0]["desc"]
        self.assertNotIn("不存在的话", desc)
        self.assertNotIn("小林", desc)
        self.assertIn("阿伟", desc)

    def test_selection_rejects_too_wide_span(self):
        msgs = [_m(f"u{i}", f"人{i}", f"原话{i}", 1000 + i * 60) for i in range(25)]
        source, _ = build_transcript(msgs, max_messages=25)
        story = {
            "events": [
                {
                    "topic": "测试",
                    "desc": "测试",
                    "ids": ["m001", "m002", "m025"],
                }
            ]
        }
        self.assertIsNone(
            normalize_selection(story, source, "测试群", max_span_messages=20)
        )


class RenderTests(unittest.TestCase):
    def test_render_with_real_avatar_files(self):
        story = {
            "title": "测试群今日趣事",
            "group": "测试群",
            "time": "10-28 19:20",
            "window_minutes": 120,
            "total_messages": 20,
            "total_people": 4,
            "events": [
                {
                    "topic": "抢券翻车现场",
                    "desc": "阿伟炫耀优惠券，结果链接失效。",
                    "heat": 8,
                    "participants": 3,
                    "time": "18:32–18:37",
                    "dialogues": [
                        {"name": "阿伟", "text": "我抢到一张 20 块的券！"},
                        {"name": "小羊", "text": "链接呢？"},
                        {"name": "乐乐", "text": "牛啊，分我一半"},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            av = base / "a.png"
            Image.new("RGB", (80, 80), (220, 80, 40)).save(av)
            out = base / "out.png"
            render_summary(story, str(out), avatars={"阿伟": str(av)})
            self.assertTrue(out.is_file())
            self.assertGreater(out.stat().st_size, 5000)
            with Image.open(out) as im:
                self.assertEqual(im.width, 820)
                self.assertGreater(im.height, 400)


if __name__ == "__main__":
    unittest.main()
