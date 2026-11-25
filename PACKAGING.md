# 打包 / Packaging

本仓库以源码形式发布，不包含 `data/`、`avatars/`、`plugin_data/`、`__pycache__`、`.bak_*` 或任何运行产物。

发布前检查：

```bash
python -m compileall -q plugins
python -m unittest discover -s plugins/astrbot_plugin_dsh_bridge/tests -v
python -m unittest discover -s plugins/astrbot_plugin_menu/tests -v
python -m unittest discover -s plugins/astrbot_plugin_group_summary/tests -v
```

再扫描密钥前缀、本机绝对路径、群号/openid、真实成员名，确认 0 命中。
