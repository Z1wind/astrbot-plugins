# emotion_route

AstrBot 情感路线注入插件。

- 在每轮 LLM 请求前按关键词识别 `crisis` / `comfort` / `advance` / `mixed`；
- 只向当前请求注入临时提示，不写入历史；
- 普通闲聊、基金等不注入；crisis 路线包含安全边界与求助热线。

无外部依赖。可单独运行 `router.py` 做分类自检。
