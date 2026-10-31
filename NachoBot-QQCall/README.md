# NachoBot QQCall

QQCall 为 NachoBot 提供 Windows QQ 私聊通话与群语音接入，通过 NapCat 或 SnowLuma 的已登录机器人账号工作。它包含独立通话控制服务、AVSDK Host、Bridge，以及 Core 内置插件；语音处理复用 UniversalVC 和 Core 的 ASR、TTS 链路。

默认关闭。使用前需要登录机器人 QQ，并准备好模型服务和本机虚拟音频路由。

## 支持范围

- **系统与 QQ**：Windows x64；当前原生绑定针对 QQNT `9.9.26-44343`，并校验对应原生文件摘要。其他 QQ 构建不能直接视为兼容。
- **适配器**：NapCat、SnowLuma。默认自动发现，多个可用候选中优先 NapCat；已建立的活动绑定不会被后来上线的适配器抢占。
- **通话**：私聊来电按策略接听；群语音支持指令加入、退出，以及经过验证的群通话邀请处理。
- **语音回复**：通话 TTS 默认中文，绕过 MCP 工具判定；普通聊天的语种设置不受影响。私聊上下文优先使用来电者真实 QQ 昵称。
- **准入**：接听或加入前检查 Core、TTS 实际合成能力、当前模式所需的 ASR/感知服务和 UniversalVC 链路；缺失或不可用时拒绝并提示。适配器已有名单和忙线规则继续生效。

控制服务显示 `ready` 表示控制链就绪，不代表已经完成真实音频回传、插话或多人通话验收。

## 首次部署

以下命令均从完整工作区根目录执行，即同时包含 `NachoBot`、`NachoBot-QQCall` 和各适配器的目录。

### 1. 准备依赖

保持仓库的相邻目录布局。Core 与适配器通过本地依赖引用 QQCall、UniversalVC 的轻量 SDK。

```powershell
uv sync --project .\NachoBot --locked

# 根据实际使用的适配器选择其中一条：
uv sync --project .\NachoBot-Napcat-Adapter --locked --extra qqcall-audio
uv sync --project .\NachoBot-SnowLuma-Adapter --locked --extra qqcall-audio
```

此外需要：

- 可用的 `uv`、Node.js，以及所选适配器的正常消息连接。
- `NapCat.Shell` 中的 `NapCatWinBootMain.exe`、`NapCatWinBootHook.dll` 和 `qqnt.json`。SnowLuma 模式也复用这些独立 Host 启动文件。
- 已部署的 UniversalVC 环境、Core 所需的 ASR/感知服务和 TTS 服务。QQCall 不另行复制模型。
- 本机活动、唯一且可验证的虚拟音频端点。

仓库已包含正式 Recovery2 Shim 和音频路由 guard 的 Windows x64 预编译文件。通常无需编译；源码重建和工具链要求见 [部署文档](docs/deployment.md)。

### 2. 生成配置并启用

QQCall 是 Core **内置插件**，源码位于 `NachoBot/src/plugins/built_in/qqcall_plugin`，无需另装插件。

启动一次 Core，插件会在文件缺失时自动生成：

```text
NachoBot/src/plugins/built_in/qqcall_plugin/config.toml
```

默认值由代码维护，已移除 `config.example.toml`。已有配置不会被覆盖；配置损坏时会记录错误并禁用插件。

在 `NachoBot/config/bot_config.toml` 中设置机器人账号，并开启统一开关：

```toml
[bot]
qq_account = "你的机器人QQ号"

[qqcall]
enabled = true
```

这是**现有配置项的示意**：修改已有 `[bot]`、`[qqcall]` 表，不要重复添加表头。默认 `enabled = false`。此开关同时控制 Core 插件、适配器通话能力、音频和控制服务；旧插件配置中的 `[plugin].enabled` 不再决定启用状态。

配置分工如下：

| 文件 | 内容 |
| --- | --- |
| `NachoBot/config/bot_config.toml` | `[bot].qq_account` 与 `[qqcall].enabled` |
| `NachoBot/src/plugins/built_in/qqcall_plugin/config.toml` | 适配器选择、连接参数、路径、通话策略、音频与 UniversalVC 设置 |

多数情况下无需重复填写账号和连接信息：

| 插件配置项 | 默认行为 |
| --- | --- |
| `qqcall.adapter` | `"auto"`，核验在线 NapCat/SnowLuma，优先 NapCat |
| `qqcall.account_id` | 留空或省略时继承 `[bot].qq_account`；显式填写且冲突时报错 |
| `qqcall.identity_file` | 留空或省略时使用适配器核验后的登录身份 |
| `qqcall.onebot` | 自动模式下复用目标适配器已有的本机接口与凭据 |
| `qqcall.paths.qq_exe` | 留空时从运行中的 QQ 进程发现安装路径；多个安装路径时需手动指定 |
| `qqcall.paths.loader_root` / `python_exe` / `node_exe` | 使用仓库布局与 PATH 默认值，可手动覆盖 |
| `qqcall.audio.capture_device` / `playback_device` | 可选设备名称覆盖，仍须通过本机路由验证 |
| `qqcall.universal_api.url` | 默认 `http://127.0.0.1:8016` |
| `qqcall.universal_api.token_file` | 留空时解析或生成私有运行令牌，不回写公共配置 |
| `qqcall.policy.auto_accept` | 默认 `false`；需要私聊自动接听时显式开启 |
| `qqcall.peer_map` | 对方 UID 与 QQ 号的映射补充，不是通话白名单；仍需核验 |

自动补齐发生在读取时，不把机器人账号或自动发现结果重复写回配置。实际绑定仍核对账号、监听端口、PID 和进程创建时间。

### 3. 准备本机音频路由

先查看设备并检查准备结果，再创建缺失的私有 guard 配置：

```powershell
.\NachoBot\.venv\Scripts\python.exe .\NachoBot-QQCall\scripts\prepare-audio-route.py --list-devices
.\NachoBot\.venv\Scripts\python.exe .\NachoBot-QQCall\scripts\prepare-audio-route.py
.\NachoBot\.venv\Scripts\python.exe .\NachoBot-QQCall\scripts\prepare-audio-route.py --apply
```

若返回 `audio_route_selection_required`，在插件配置的 `[qqcall.audio]` 中填写对应的 `capture_device`、`playback_device` 名称，再重新检查。

已有 guard 配置只验证、保留。设备身份属于当前机器，不能复制其他机器的 endpoint ID；没有有效虚拟路由时不会回退到物理麦克风或 Windows 默认设备。

### 4. 启动与绑定

1. 启动所需模型服务、Core 和选定的 QQ 适配器，登录机器人 QQ。修改启用开关后需重启 Core；账号与连接配置变更也需要重启相关适配器和服务。
2. 运行固定入口：

   ```powershell
   .\NachoBot-QQCall\start-qqcall.bat
   ```

3. 若弹出 UAC，确认本次绑定；等待状态变为 `ready`。

BAT 会复用有效的 supervisor，必要时启动新的控制服务，并为当前代次申请绑定。已就绪时不会重复启动或注入。无需查找 `connection-N` 目录，也不依赖 WebUI API。

WebUI 的 QQCall 卡片使用“注入”按钮，调用同一个 BAT，并识别外部启动的控制服务。

WebUI 在启动或注入前检查 UniversalVC 的独立 Python 环境、必要原生依赖、实际配置的 VAD/声纹模型，以及服务器上的虚拟声卡和音频 guard 配置。降噪不作为启动的强制条件。检查失败会阻止启动，在卡片及控制台列出问题，并提示前往部署向导部署或修复 Universal 服务。检查不会自动安装依赖、下载模型或修改音频配置；首次部署仍需完成上面的本机音频路由准备。

## 使用通话

| 指令 | 使用位置与作用 |
| --- | --- |
| `#join_vc_qq` | 在 QQ 群中请求加入该群已存在的语音房间 |
| `#leave_vc_qq` | 在 QQ 群中请求离开当前群语音 |
| `#stat_vc_qq` | 查询当前群语音或私聊通话状态 |

群加入需要新鲜、身份匹配的群成员消息，以及适配器名单、房间存活和通话空闲检查。收到入房原生成功回调后才确认已加入；发送请求不等于成功入房。

用户发起群通话并邀请机器人时，邀请处理同样受身份、房间、忙线和依赖链路检查约束。私聊自动接听还需开启 `qqcall.policy.auto_accept`，并满足来电关联与准入条件。

当前没有开放式自然语言加入入口。旧固定短语“请加入群语音”“加入当前群通话”“加入群语音”不再注册为指令；`#qqcall accept`、`#qqcall hangup`、`#qqcall reject` 也已删除。

## 查看状态与停止

```powershell
.\NachoBot-QQCall\scripts\qqcall-service.ps1 check
.\NachoBot-QQCall\scripts\qqcall-service.ps1 plan
.\NachoBot-QQCall\scripts\qqcall-service.ps1 status
.\NachoBot-QQCall\scripts\qqcall-service.ps1 stop
```

| 操作 | 含义 |
| --- | --- |
| `check` | 检查配置与静态依赖，包括必要的 QQ 路径发现 |
| `plan` | 只读检查适配器、登录身份和目标进程 |
| `status` | 读取当前控制服务的脱敏状态 |
| `stop` | 请求当前服务受控停止、解绑，并清理其拥有的 Host/Bridge |

需要单独前台运行控制服务时，可执行 `qqcall-service.ps1 run`；它不会自行完成管理员注入，后续仍使用固定 BAT 绑定。

supervisor 控制台显示带时间的状态变化日志；重复轮询不会刷屏。新启动的 UniversalVC 使用独立可见控制台，复用已有实例时不会额外打开窗口。入口 BAT 成功后退出，失败时暂停。

UniversalVC 是共享服务：QQCall 会优先复用符合认证和就绪条件的实例；符合本机启动条件且没有服务时，可使用其已有环境启动。停止 QQCall 后，UniversalVC 仍保持运行。

## 常见问题

控制台关闭后，可查看 `.runtime/service/status-history.private.json` 中最近 64 次状态变化。记录包含时间、控制服务进程身份、阶段和错误码，跨服务重启保留；不会保存通话内容、配置或原始异常文本。遇到 `generation_failure_limit` 时，查看它前面的错误码以定位触发重试的原因。历史记录不代表对应进程仍在运行。

Bridge 未捕获异常会在对应 `generations/connection-N/agent-report.controller-exit.private.json` 中留下退出证据，包括异常类型、已知系统错误码，以及允许记录的 QQCall 源文件相对路径和行列号。未知类型或错误码会被归类，原始异常文本、函数名和绝对路径不会保存。此诊断不改变 Node 的异常退出行为。

正常停止时控制服务会释放其拥有的 Host。Windows CLI 启动时会先建立进程生命周期 Job，使其 Host/Bridge 子进程在控制服务正常或异常退出时由系统回收；固定 BAT 启动也具备此保障。共享 UniversalVC 通过已有的显式 breakaway 标志保持独立，现有机器人和拨号 QQ 不会加入该 Job。无法建立 Job 时启动报 `service_process_job_unavailable`，不继续创建服务子进程。

对于旧版本留下的进程，再次启动或执行 `stop` 会检查保留的归属记录：只有旧控制服务和 Bridge 均已退出，才按记录的 PID、创建时间和程序路径清理 Host；清理未确认时保留记录并拒绝继续启动。旧 Bridge 仍存活、进程状态无法核验或归属记录已丢失时，不会仅凭端口或 `QQ.exe` 名称结束进程。

| 现象或状态 | 处理方式 |
| --- | --- |
| 插件未启用、指令不可用 | 检查 bot config 的 `[qqcall].enabled` 和 Core 插件加载日志；修改后重启 Core |
| `bot_account_mismatch` | 插件中显式账号与 `[bot].qq_account` 不一致；移除重复字段或修正账号 |
| `qq_executable_not_found` | 登录机器人 QQ，或显式填写 `qqcall.paths.qq_exe` |
| `qq_executable_ambiguous` | 检测到多个 QQ 安装路径；显式指定机器人所用路径 |
| `waiting_adapter` / `waiting_login` | 检查适配器连接、机器人登录状态和账号是否匹配 |
| `needs_attachment` | 使用 `start-qqcall.bat` 完成当前代次的 UAC 绑定 |
| `service_not_startable_waiting_kernel_restart` | 一次性 Shim 已消费且连接无法恢复；停止服务、重启并登录机器人 QQ，再运行固定 BAT |
| `startup_timeout_check_core_adapter_and_robot_login` | 查看 supervisor 状态，检查 Core、适配器、机器人登录及依赖链路 |
| TTS / UniversalVC / ASR 未就绪 | 按提示修复对应服务，再重新拨打或邀请；端口监听和模型已加载不等于链路就绪 |
| `audio_route_selection_required` 或路由验证失败 | 重新查看本机虚拟端点和 guard 准备结果，确认设备名称与绑定一致 |
| 发出离开请求但仍占用通话槽 | 查看原生终态和成员查询结果；请求 ACK 或音频停止本身不证明机器人已离开 |

适配器短暂掉线时，服务会暂停通话准入，并尝试在原 QQ 进程、账号和路由重新核验后恢复健康绑定。已失效的一次性 Shim 不能靠重启 Python 服务或删除隔离记录复活，需要新的机器人 QQ 进程。被中断的通话不保证自动续接。

群管理中的“结束通话”与成员个人退出不同。全局结束后，QQCall 会核验原生房间状态并清理群音频及占用；正常清理后可在同一绑定上接受新的邀请。拨号者个人退出或关闭 QQ 时，群通话可能仍在继续，机器人留在房间内不等于占用泄漏。需要机器人离开时使用 `#leave_vc_qq`，或由管理员全局结束通话。

群通话中若 Windows 音频路由检测发生一次超时，系统会在原有两秒证明期限内尝试一次完整复核，等待旧检测进程退出后再创建替代进程。持续超时、路由不匹配或通话身份失效仍会停止音频，不延长旧证明的有效期。邀请自动加入必须收到并验证 QQ 原生邀请；只有人数变化时不会据此自动入房。邀请未到达时可先查询 `#stat_vc_qq`，确认房间状态后再用 `#join_vc_qq`。

## 项目结构与验证

| 目录 | 职责 |
| --- | --- |
| `service/` | supervisor、配置校验、适配器发现、身份与进程绑定 |
| `bridge/`、`agent/` | 通话状态机、QQ 原生事件与受控 IPC |
| `host/` | 独立 AVSDK Host |
| `audio/`、`qqcall_pcm/` | Windows 音频路由和 PCM 传输 |
| `sdk/qqcall_client/` | Core、NapCat、SnowLuma 共用的轻量客户端 |
| `native/` | Shim、音频 guard 及构建信息 |
| `scripts/` | 固定启动入口、依赖选择与本机准备工具 |

UniversalVC 的轻量 SDK 位于相邻项目 `NachoBot-UniversalVC-Adapter/sdk`；`ncnk_message` 只负责通用消息结构与传输。

配置测试、静态检查、镜像构建和控制服务就绪均不替代真实通话验证。发布或更换 QQ 构建后，应验证实际接听/入房、双向音频、远端 TTS、插话、多人说话、结束释放和重连恢复。

- [部署与原生构建](docs/deployment.md)
- [共享 SDK 与容器构建](sdk/README.md)
- [音频路由实现](native/audio_route/README.md)
- [内置插件说明](../NachoBot/src/plugins/built_in/qqcall_plugin/README.md)
- [历史探针记录](docs/historical-probes.md)

本机 `config.toml`、`.runtime`、登录数据与私有 token 不应作为部署包或公开日志分享。
