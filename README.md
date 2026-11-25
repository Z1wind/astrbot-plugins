# AstrBot 原创插件合集

本仓库收录 7 个在 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 上实际使用、维护过的插件。
其中「自建」是围绕本机机器人新写的插件；「定制」是在公开插件基础上按本机需求改过的版本，均已在目录中标注。

> 公开版已移除 API key、cookie、QQ 群号/openid、真实成员名、头像文件和本机绝对路径。
> 所有凭据都请在 AstrBot WebUI 或环境变量里配置，不要提交到仓库。

## 插件

| 插件 | 类型 | 功能 | 主要命令 |
|---|---|---|---|
| `astrbot_plugin_dsh_bridge` | 自建 | 通过 QQ 私聊让本机 DeepSeek Harness 跑 headless 任务；支持会话选择、续写、心跳、推理摘录、`/dsh stop` | `/dsh <任务>`、`/dsh sessions`、`/dsh use N`、`/dsh new` |
| `astrbot_plugin_emotion_memory` | 自建 | 情感对象档案：LLM 工具增删查 + Dashboard 管理页 | LLM 工具 `emotion_memory_save/query/list` |
| `astrbot_plugin_emotion_route` | 自建 | 按关键词识别情感意图，向当轮 LLM 请求注入 crisis/comfort/advance/mixed 路线 | 无命令，自动触发 |
| `astrbot_plugin_menu` | 自建 | 面向使用者的 `/菜单`，按聊天/情感/基金/群开关/工具/管理员分组 | `/菜单`、`/helpall`、`/功能` |
| `astrbot_plugin_group_summary` | 定制 | 群趣事总结：LLM 按话题连贯性选原话，生成带头像的聊天漫画；可只读本机 QQ 历史补全 | `@机器人 总结`、`@机器人 趣事` |
| `astrbot_plugin_tg_watch` | 自建 | Telegram 增量监听：统计新消息、检测关注用户是否发言并出卡片 | `/tg`、`/tg status` |
| `astrbot_plugin_sheep_fund_rank` | 定制 | 群基金涨跌榜：成员持仓/买入卖出、实时估值、每日播报和管理面板 | `涨跌榜`、`导入数据` 等 |

## 安装

1. 把需要的插件目录放到 AstrBot 的 `data/plugins/` 下；
2. 在 WebUI 插件管理页启用并填写配置；
3. 热重载或重启 AstrBot。

通用要求：Python 3.10+、AstrBot 4.24+。部分插件另需 `httpx`、`Pillow`、`Telethon` 或本机工具，见各插件 README。

## 安全与隐私

- 本仓库**不包含**任何 API key、cookie、QQ 群号/openid、真实成员名或头像文件。
- `dsh_bridge` 等价于远程控制本机：只给管理员私聊使用，不要开放到群。
- `group_summary` 的本地历史源只应读取自己的群；默认群号/openid 在公开版里留空。
- `sheep_fund_rank` 的成员昵称 -> QQ 映射在公开版里为空，请在你的本地副本中维护，不要提交真实映射。
- 发现安全问题请按 [SECURITY.md](SECURITY.md) 私下联系，不要公开 Issue。

## 开发与验证

```bash
python -m compileall -q plugins
python -m unittest discover -s plugins/astrbot_plugin_dsh_bridge/tests -v
python -m unittest discover -s plugins/astrbot_plugin_menu/tests -v
python -m unittest discover -s plugins/astrbot_plugin_group_summary/tests -v
```

贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，来源与许可见 [ATTRIBUTIONS.md](ATTRIBUTIONS.md)。

## License

MIT，见 [LICENSE](LICENSE)。
