# astrbot_plugin_dsh_bridge · DSH 远程任务桥

通过 QQ 私聊，把任务交给本机的 **DeepSeek Harness** 跑，再把最终结果发回来。
可以每次新建对话，也可以选中一个已有工作区对话续写。

> 这是远程控制电脑的入口：DSH 会以你的 Windows 账户读写文件、执行命令。只给管理员、只在私聊开。

## 命令

| 命令 | 作用 |
|---|---|
| `/dsh <任务>` | 新建对话跑一次；如果已选中会话，就续在它的历史里；preset 不兼容时自动新建 Default 会话并带最近上下文 |
| `/dsh sessions` | 列出 DSH 最近对话（标题、工作区、最后更新、是否可能在用） |
| `/dsh use <序号>` | 选中列表里的第 N 个对话 |
| `/dsh use session-…` | 直接按会话 ID 选中 |
| `/dsh new` | 取消选中，回到每次新建 |
| `/dsh status` | 查看桥状态（CLI、provider、当前会话、工作目录、忙闲） |
| `/dsh stop` | 终止当前正在跑的任务 |
| `/dsh help` | 用法 |

别名：`/远程任务`。

## 它怎么工作

```text
QQ 私聊 /dsh ...
  → AstrBot 插件校验：管理员 + 私聊
  → 读 cmd_config.json，取当前启用 provider 的 key / api_base
  → 若选中了会话，加上 --session-id，cwd 用该会话的工作区
  → 子进程：
      "DeepSeek Harness.exe" --expose-internals <cli.js> \
        --profile headless [--session-id <id>] "<任务>"
    env: ELECTRON_RUN_AS_NODE=1
         DEEPSEEK_API_KEY=<key>
         DEEPSEEK_BASE_URL=<api_base>
  → stdout 的最终回答 → QQ
```

- `headless` 是 DSH 自带的 profile；不带 `--session-id` 时每次新建。
- 带 `--session-id` 时读入该对话的持久化历史，任务会追加到同一个会话（已实测：先让它记 42，再续会话问，回答 42）。
- 会话列表来自 `~/.dsh/storages/session_projcache/sessions/*.json`：标题取 `record.rows.title.val`，工作区取 `record.identity.cwd`，更新时间取文件 mtime；归档会话（`workspace.json` 的 `archivedSessionIds`）不列出。
- DSH 没装进 PATH，所以直接调 Electron + asar 里的 `cli.js`。
- key 只放子进程环境变量，不落盘、不写日志。
- 默认工作目录：`~/Documents/deepseek-harness/default-workspace`；没选会话时可用环境变量 `DSH_BRIDGE_WORKDIR` 改。

## 选中会话的例子

```text
/dsh sessions
  1. 基金插件｜default-workspace｜10-27 17:29 ⏳
  2. Minecraft｜default-workspace｜10-27 17:29 ⏳
  3. 只回复 pong｜default-workspace｜10-27 17:18

/dsh use 3
  已选中：只回复 pong
  工作区：C:\...\default-workspace
  之后 /dsh <任务> 会续在这个对话里；/dsh new 取消。

/dsh 继续刚才的活，只改测试
```

- 序号只对**最近一次 `/dsh sessions`** 有效；插件重启后要重新列。
- `⏳` 表示 5 分钟内更新过，可能正在 GUI 里用；同时写可能冲突。
- 选中状态按 QQ 发送者存在 `~/.astrbot/data/plugin_data/astrbot_plugin_dsh_bridge/state.json`，重启后还在。
- 续会话会把历史一起带进去，token 更多、也更容易被旧上下文带偏；不确定就 `/dsh new`。

## 等待与进度

- 单次默认等 **3600 秒（1 小时）**；`DSH_BRIDGE_TIMEOUT=7200` 可改成 2 小时（秒）。
- 运行中默认每 **60 秒**发一条：

  ```text
  还在跑：已用 3 分 00 秒，进程存活。
  ```

  间隔用 `DSH_BRIDGE_HEARTBEAT=120` 改；设 `0` 关闭。
- **推理摘录**（`🧠`）默认最多每 **120 秒**一条、一次任务最多 **4 条**；`DSH_BRIDGE_REASONING_INTERVAL` / `DSH_BRIDGE_REASONING_MAX` 可改，`MAX=0` 关闭实时摘录。结束时补一段最后摘录。
- 心跳报告的是 **进程存活 / 已用时间**；推理摘录来自 DSH 的 stderr，可能滞后/不完整，不是精确进度。
- 到超时会杀进程，回 `DSH 超过 N 秒，已终止。`
- 正常结束后结果末尾带 `（用时 X 秒）`；`/dsh stop` 后只回“已手动终止”，不再把退出码 1 / crashpad 噪声当成错误。

## 护栏

- **仅管理员**（`event.is_admin()`），**仅私聊**（`event.is_private_chat()`）；群里直接拒绝。
- 单次默认超时 **3600 秒（1 小时）**，到点杀进程；可用 `DSH_BRIDGE_TIMEOUT` 覆盖。
- 运行中每 **60 秒**报告一次存活；可用 `DSH_BRIDGE_HEARTBEAT` 覆盖，`0` 关闭。
- 推理摘录每 **120 秒**最多 1 条、一次任务最多 **4 条**；可用 `DSH_BRIDGE_REASONING_INTERVAL` / `DSH_BRIDGE_REASONING_MAX` 覆盖。
- 任务最多 **2000 字**，输出截断 **6000 字**。
- 同一时间只跑一个任务；忙时返回提示。
- 审计日志：`~/.astrbot/data/plugin_data/astrbot_plugin_dsh_bridge/audit.jsonl`
  - 记录时间、发送者、umo、会话 ID、工作区、任务前 500 字、退出码、耗时、是否超时
  - 不记录完整输出
- 不提供 `/pc <任意 shell>`；但自然语言任务仍可让 DSH 执行命令，这是设计目标，也是风险来源。
- 会话标题/工作区只发给管理员私聊，不往群里发。

## 安装 / 部署

```powershell
.\scripts\deploy.ps1 -Plugins -Only astrbot_plugin_dsh_bridge -Apply -Restart
```

需要本机已安装 DeepSeek Harness（默认 `%LOCALAPPDATA%\Programs\DeepSeek Harness`），且 AstrBot 当前 provider 有可用 key。

## 已知边界

- 一次一个任务；不能同时在两个会话里跑。
- 会话列表和自动接续的摘要都读 DSH 的**缓存 JSON**，不是官方 API；DSH 改格式要跟着改 `dsh_runner.py`。
- 自动接续只带最近 2 轮摘要，不是完整历史；需要完整上下文时，仍建议在 GUI 里把原会话 preset 调成 Default 后直接用 `/dsh use` 选它。
- 如果选中的会话正在 GUI 里用，可能冲突；插件只警告，不阻止。
- 结果是最终消息；`🧠` 推理摘录只是 stderr 片段，不含完整工具调用。
- 进度报告只看进程/粗略推理，进程活着不等于任务没卡住。
- QQ 侧等待可能 5 秒–1 小时（DSH 启动 + 模型推理 + 工具调用），先发“收到”回执再等结果。
- 选中的会话如果用了非 **Default** 的 agent preset（如 `cordis`/`standard`），headless 续不了。插件不再让用户去 GUI 改：会自动新建 Default 会话，并从原会话 GUI 缓存里取最近 2 轮摘要带过去（最多约 1800 字）；跑完自动选中新会话，后续任务续在新会话里。若新会话没写进缓存，会提示“没定位到新会话”，下次仍会自动接。
- 如果 DSH 升级后 asar 内 `cli.js` 路径变了，需要同步改 `dsh_runner.py` 的 `CLI_REL`。
- 超时只杀 DSH 父进程，不保证杀掉它派生的子进程。
