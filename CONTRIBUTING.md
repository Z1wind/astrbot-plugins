# 贡献指南 / Contributing

## 中文

1. Fork 并创建分支（`feat/xxx`）；
2. 先跑基线：

   ```bash
   python -m compileall -q plugins
   python -m unittest discover -s plugins/astrbot_plugin_dsh_bridge/tests -v
   python -m unittest discover -s plugins/astrbot_plugin_menu/tests -v
   python -m unittest discover -s plugins/astrbot_plugin_group_summary/tests -v
   ```

3. 改代码请测试先行，PR 附真实命令输出；
4. 不提交密钥、cookie、群号、真实成员资料、`data/`、`avatars/`、`__pycache__`、`.bak_*`；
5. PR 说明：改了什么、怎么验证、限制。

## English

1. Fork and branch (`feat/xxx`).
2. Run the baseline commands above.
3. Test-first changes, with real command output in the PR.
4. Never commit secrets, cookies, group IDs, real member data, `data/`, `avatars/`, `__pycache__`, or `.bak_*`.
5. Describe what changed, how it was verified, and remaining limitations.
