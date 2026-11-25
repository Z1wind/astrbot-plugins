# 安全策略 / Security Policy

## 不要提交的内容

- API key、cookie、token、密码、私钥；
- QQ 群号 / group openid / 真实成员名 / 头像；
- 本机绝对路径、运行日志、`plugin_data/`、`data/`、数据库快照。

本仓库公开版已做移除；使用时请把凭据放在 AstrBot WebUI 或本机环境变量中。

## 高风险插件

`astrbot_plugin_dsh_bridge` 会让本机 DeepSeek Harness 执行任务，等价于远程控制电脑。只允许管理员、只允许私聊；不要开放给群或不可信用户。

## 报告问题

请勿公开 Issue。请私下联系仓库维护者，附复现步骤和影响范围。不要附带真实 cookie、key 或聊天记录。
