# -*- coding: utf-8 -*-
"""菜单文案，纯函数，方便单测；不依赖 AstrBot。"""
from __future__ import annotations


def build_menu(is_admin: bool = False, is_private: bool = True) -> str:
    where = "私聊直接说话就行" if is_private else "群里要先 @机器人，再说话"
    lines = [
        "AstrBot 功能菜单",
        "",
        f"聊天 / 情感：{where}；烦心事、感情问题直接讲，我会先接住再判断。",
        "基金：直接问，或 @我 说「涨跌榜」；/帮助 看导入、买入、卖出等完整用法。",
        "",
        "群里的开关（@我 后单独发）：",
        "  闭嘴 / 说话 / 爬楼",
        "",
        "小工具（@我 后发）：",
        "  涨跌榜        群基金涨跌 / 持仓",
        "  验车 <磁力>   种子、磁力信息分析",
        "  弗一把        猜 CS 选手",
        "  op无人        OOPZ 在线人数",
        "  反查 / 识人   截图反查（发图后说）",
        "",
    ]
    if is_admin and is_private:
        lines += [
            "管理员（仅私聊）：",
            "  /dsh <任务>     让本机 DSH 干活，结果回这里",
            "  /dsh sessions   列最近对话",
            "  /dsh use N      选中第 N 个，后续任务续写",
            "  /dsh new        取消选中",
            "  /dsh status     看桥状态；/dsh help 看完整用法",
            "  /拉黑 @人       不再响应；/取消拉黑、/黑名单",
            "  /help           AstrBot 内置命令",
            "  /reset /new /stop /sid /stats   会话与调试",
            "",
        ]
    elif is_admin:
        lines += [
            "管理员命令（/dsh 等）请私聊我。",
            "",
        ]
    lines += [
        "已下线：/crush 暗恋排练（2026-10-27 卸载）。",
        "说明：/help 只列系统命令；/菜单（/helpall、/功能）才是机器人的功能入口。",
    ]
    return "\n".join(lines)
