# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from menu_text import build_menu  # noqa: E402


class MenuTests(unittest.TestCase):
    def test_public_menu_hides_admin(self):
        text = build_menu(is_admin=False, is_private=True)
        self.assertIn("AstrBot 功能菜单", text)
        self.assertIn("涨跌榜", text)
        self.assertNotIn("/dsh <任务>", text)
        self.assertNotIn("/拉黑", text)

    def test_admin_menu_shows_dsh(self):
        text = build_menu(is_admin=True, is_private=True)
        self.assertIn("/dsh <任务>", text)
        self.assertIn("/dsh sessions", text)
        self.assertIn("/dsh use N", text)
        self.assertIn("/dsh new", text)
        self.assertIn("/dsh status", text)
        self.assertIn("/dsh help", text)

    def test_admin_group_does_not_advertise_dsh(self):
        text = build_menu(is_admin=True, is_private=False)
        self.assertIn("管理员命令", text)
        self.assertNotIn("/dsh <任务>", text)
        self.assertNotIn("/dsh sessions", text)

    def test_fund_help_mention(self):
        self.assertIn("/帮助", build_menu(is_admin=False, is_private=True))

    def test_group_wording(self):
        self.assertIn("@机器人", build_menu(is_admin=False, is_private=False))
        self.assertIn("私聊直接说话", build_menu(is_admin=False, is_private=True))

    def test_offline_crush_note(self):
        self.assertIn("/crush", build_menu(is_admin=False, is_private=True))


if __name__ == "__main__":
    unittest.main()
