# AstrBot Plugins

Seven AstrBot plugins that were actually used and maintained locally: four self-built plugins and three customized ones. The public edition has no API keys, cookies, group IDs/openids, real member names, avatars or absolute local paths.

| Plugin | Type | Purpose |
|---|---|---|
| `astrbot_plugin_dsh_bridge` | self-built | Run local DeepSeek Harness headless tasks from QQ private chat, with session resume/heartbeat/stop |
| `astrbot_plugin_emotion_memory` | self-built | Emotion-memory profiles with LLM tools and a dashboard page |
| `astrbot_plugin_emotion_route` | self-built | Inject crisis/comfort/advance/mixed guidance into the current LLM request |
| `astrbot_plugin_menu` | self-built | User-facing menu grouped by topic |
| `astrbot_plugin_group_summary` | customized | Group-story summary with LLM-selected original messages and avatar chat cards |
| `astrbot_plugin_tg_watch` | self-built | Incremental Telegram watch and card generation |
| `astrbot_plugin_sheep_fund_rank` | customized | Group fund ranking, holdings, daily broadcast and panel |

## Install

Copy the desired plugin directory into AstrBot `data/plugins/`, enable it in the WebUI, fill in local configuration, then reload or restart AstrBot.

Requirements: Python 3.10+, AstrBot 4.24+. Some plugins additionally need `httpx`, `Pillow`, `Telethon`, or local tools; see each plugin README.

## Security

No secrets or personal data are included. `dsh_bridge` is equivalent to remote control of the machine: administrator-only, private chat only. `group_summary` group IDs/openids are blank in this public edition. `sheep_fund_rank` intentionally ships with an empty member-name -> QQ mapping.

Report security issues privately per [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).
