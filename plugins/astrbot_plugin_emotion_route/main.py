# -*- coding: utf-8 -*-
"""乐乐 · 情感路线自适应注入。

按这一轮消息的意图选路线，而不是所有情感话题都塞同一段"主导"文案：
- crisis  ：自伤/自杀信号 → 安全优先、转介
- comfort ：倾诉/受伤/被吊着/被冷暴力 → 先接情绪 + 点破操控
- advance ：要追求/推进/邀约 → 主动可执行的打法 + 不操控底线
- mixed   ：又受伤又想追 → 先接情绪，再判断该进该退

分类逻辑在 router.py（纯函数，可单独跑）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Final

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star, register

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # noqa: BLE001
    TextPart = None

EXTRA_ROUTE_INJECTED: Final[str] = "_emotion_route_injected"


def _load_router():
    """按文件路径加载 router.py，避免插件包导入方式差异。"""
    path = Path(__file__).with_name("router.py")
    spec = importlib.util.spec_from_file_location("astrbot_emotion_route_router", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    _router = _load_router()
    classify = _router.classify
    ROUTES: dict[str, str] = _router.ROUTES
    logger.info("[情感路线] 分类器已加载（router.py）")
except Exception as exc:  # noqa: BLE001
    logger.error(f"[情感路线] 分类器加载失败，将不注入任何路线: {exc}")
    classify = lambda text: None  # type: ignore[assignment]
    ROUTES = {}


@register(
    "astrbot_plugin_emotion_route",
    "your_name",
    "情感路线自适应注入：危机→安全转介，倾诉→先共情，求策略→主动推进，混合→先接情绪再判断。情感路线本轮禁用口头禅/玩梗词，避免把共情回成段子。",
    "1.2.1",
)
class EmotionRoute(Star):
    def __init__(self, context) -> None:
        super().__init__(context)

    @filter.on_llm_request(priority=91)
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        try:
            text = event.message_str or ""
            route = classify(text)
            if not route:
                return
            if event.get_extra(EXTRA_ROUTE_INJECTED, False):
                return
            self._append_temp_text(req, ROUTES[route])
            event.set_extra(EXTRA_ROUTE_INJECTED, route)
            logger.info(f"[情感路线] 本轮注入 {route}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[情感路线] 注入失败: {exc}")

    def _append_temp_text(self, req: ProviderRequest, text: str) -> None:
        if TextPart is not None:
            try:
                part = TextPart(text=text)
                mark_as_temp = getattr(part, "mark_as_temp", None)
                if callable(mark_as_temp):
                    part = mark_as_temp() or part
                req.extra_user_content_parts.append(part)
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[情感路线] 临时上下文注入失败, 回退 system_prompt: {exc}")
        req.system_prompt = (req.system_prompt or "") + "\n\n" + text
