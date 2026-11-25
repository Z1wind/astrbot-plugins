# -*- coding: utf-8 -*-
"""astrbot_plugin_menu —— 乐乐的功能菜单。

AstrBot 内置的 /help 只列系统保留命令，看不到插件提供的聊天/情感/基金/群指令；
这里给一个面向使用者的 /菜单，按“聊天、群指令、小工具、管理员”分组。
"""
from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .menu_text import build_menu

PLUGIN_NAME = "astrbot_plugin_menu"


@register(
    PLUGIN_NAME,
    "astrbot-lele",
    "乐乐的功能菜单：/菜单 按聊天、群指令、小工具、管理员分组列出常用入口；/help 只列 AstrBot 系统命令。",
    "0.1.1",
)
class Menu(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    @filter.command("菜单", alias={"helpall", "功能", "菜单帮助"})
    async def menu(self, event: AstrMessageEvent):
        """查看乐乐的功能菜单"""
        yield event.plain_result(
            build_menu(event.is_admin(), event.is_private_chat())
        )
