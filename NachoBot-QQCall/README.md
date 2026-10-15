# QQCall Windows service and integration

当前文字指令为 `#join_vc_qq`、`#leave_vc_qq`、`#stat_vc_qq`。`#qqcall accept`、`#qqcall hangup`、`#qqcall reject` 已删除。自动接听及内部通话操作继续遵循原有策略。

QQCall 共享运行时已迁至 [`sdk/qqcall_client`](sdk/README.md)，UniversalVC 轻量客户端位于 `../NachoBot-UniversalVC-Adapter/sdk`。`ncnk_message` 只提供通用消息结构与传输，不再包含这两者的实现或旧路径转发。Core、NapCat、SnowLuma 已声明独立 SDK 本地依赖；更新后执行各项目的锁定依赖同步，并正常重启 Core 与当前 QQ 适配器。PCM 仍由现有可选 `qqcall-audio` extra 提供。

`[qqcall].adapter = "auto"` 自动核验在线 NapCat/SnowLuma，多个可用实例时优先 NapCat。自动模式读取目标账号已有的本机 OneBot 接口配置与凭据，不扫描任意端口或按 QQ 进程名直接注入。心跳、账号、PID/FILETIME、进程来源和一次性绑定检查仍须全部通过；活动绑定保持当前适配器，未选中的进程不能领取通话租约。仍可显式指定 `napcat` 或 `snowluma`。切换模式后需重新加载适配器和 QQCall 控制服务。

新部署的依赖、正式 Recovery2 编译、配置与固定绑定入口见 [Windows 部署与验证](docs/deployment.md)。真实账号绑定、通话、远端 TTS、打断与迟到输出、多说话人区分、恢复及持续运行仍需单独端到端验收；本地构建、SDK 回调和模拟用例不能替代这些结果。

本仓库包含一套 Windows QQCall supervisor，可用正式配置管理独立 AVSDK Host 和 Bridge。唯一公开配置位于 `NachoBot/plugins/qqcall_plugin/config.toml`；其中 `[plugin].enabled` 是 QQCall 唯一开关，适配器、音频和 supervisor 都从这份固定 TOML 派生状态。只有 canonical TOML 尚不存在时，才参考 `NachoBot/plugins/qqcall_plugin/config.example.toml` 创建它；已有配置应从现有文件读取和编辑，不要用示例文件覆盖。按需填写账号、OneBot/QQ 路径；`[qqcall.audio]` 可省略，或只填写 `capture_device` / `playback_device` 作为名称覆盖。schema 6 新增 `[qqcall.universal_api]`，默认指向本机 `http://127.0.0.1:8016`。`token_file` 留空时，启用的 supervisor 会在 QQCall 私有 runtime 中解析默认 token 文件；如果没有可复用的环境变量或现有 UniversalVC 服务 token，会首次生成该私有 token，并通过子进程环境交给 UniversalVC。已存在的 UniversalVC `service.toml` 会被读取而不会覆盖；有效的 `NACHOBOT_UNIVERSALVC_HOST`、`PORT`、`TOKEN` 环境变量优先于其中对应值。只有 HTTP loopback 根地址连接被拒绝时才尝试启动现有 `NachoBot-UniversalVC-Adapter/.venv` CLI；健康检查失败、已有监听者拒绝认证、HTTPS 自动启动不受支持或无法确认服务就绪时，QQCall 控制仍可运行，但状态会显示 UniversalVC 音频不可用。成功的 pipeline 健康检查不证明 QQ 设备路由已验证。自动启动的 UniversalVC 是可能被其他客户端复用的共享服务，会在 QQCall supervisor 停止后继续运行。schema 5 的现有 canonical 文件可先用 `python .\scripts\migrate_config.py ..\NachoBot\plugins\qqcall_plugin\config.toml` 做脱敏 dry run；需要写入时再显式传 `--apply`。不要把本地配置提交到版本库。

```powershell
./scripts/qqcall-service.ps1 check
./scripts/qqcall-service.ps1 plan
./scripts/qqcall-service.ps1 run
# 另一个终端读取脱敏状态或请求受控停止
./scripts/qqcall-service.ps1 status
./scripts/qqcall-service.ps1 stop
```

`check` 只验证配置，`plan` 只读检查 adapter、OneBot 身份与目标进程，`run` 前台运行 supervisor，`status` 读取稳定脱敏状态，`stop` 发出绑定到当前 manager PID、FILETIME 和 generation 的优雅停止请求。默认禁用配置不会创建 `.runtime`、启动进程或绑定端口。启用后，run 模式会按需通过 UniversalVC 公共 CLI 启动或复用共享 UniversalVC 服务；停止时只会停止由本 supervisor 创建的 QQCall Host 和 Bridge，并让 UniversalVC 保持运行；它不会启动/重启普通机器人 adapter、个人 QQ 或自行运行管理员注入脚本。

Windows 日常启动可双击本目录的 `start-qqcall.bat`。先启动 Core、所选适配器并登录机器人；WebUI 可选。该固定入口复用身份仍有效的 supervisor，没有运行实例时通过 `qqcall-service.ps1 run` 在独立可见控制台启动 supervisor，不请求 WebUI API。随后等待当前连接准备完成，在 `needs_attachment` 时自动申请一次 UAC 权限并调用这一代生成的绑定脚本，最后等待 `ready`。无需手动查找变化的 `connection-N` 路径。已经 ready 时不会重复启动、升权或绑定；取消 UAC、绑定期间代次改变、失败或超时会明确退出，不会改绑新的 QQ 进程。原有 PID/FILETIME、进程图像、DLL 摘要和单次尝试校验均保留。此入口只处理 QQCall，不会启动或关闭个人 QQ，也不会代替机器人登录。独立实例可通过 `qqcall-service.ps1 status` 查看、通过 `qqcall-service.ps1 stop` 优雅停止。

QQCall 启动链默认显示控制台：supervisor 和 UAC 注入子进程采用普通窗口，自动新建的 UniversalVC 服务使用独立控制台并显示日志；WebUI 注入按钮也打开可见的 BAT 窗口。已有服务被复用时不会补建窗口，需该服务正常重启后应用新的启动方式。入口 BAT 成功完成后仍会退出，失败时暂停。

supervisor 的 `run` 控制台会实时输出带时间的中文状态日志：启动、等待适配器/登录/身份验证、等待注入、就绪、恢复、异常和停止。UniversalVC 就绪、复用或不可用也会显示；相同状态轮询不重复刷屏。日志写入标准错误并立即刷新，标准输出的最终 JSON 和 `status.json` 格式保持不变。日志仅使用已过滤的公共状态，不输出令牌、原始消息或音频。已有进程需正常重启后加载该代码；不要仅为加载日志强制重启正在使用的注入代次。

可用 `powershell -NoProfile -File .\scripts\start-qqcall.ps1 -CheckOnly` 只读检查状态。WebUI 的 QQCall 卡片使用“注入”按钮，调用同一固定 BAT；已注入时禁用，不提供通用“启动组/重启组”。WebUI 会校验独立 supervisor 的 PID/FILETIME、运行目录、启动脚本和 canonical 配置路径，识别外部启动并阻止重复拉起，且不接管其进程。通过 `qqcall-service.ps1 run` 单独启动的控制服务仍会等待管理员绑定，可以再运行固定入口复用该 supervisor 完成这一阶段。

配置由 Core、所选 adapter 和 supervisor 在各自启动时读取。改动 `[plugin].enabled`、账号、身份或策略配置前先执行 `stop`，编辑后重启 Core、所选 adapter 和 supervisor；这些进程不会热加载配置，也不会在活动会话内改变身份映射。音频状态严格跟随 `[plugin].enabled`，音频配置没有单独开关。启用时，supervisor 从私有 native route guard 已固定的活动 endpoint ID 解析名称，并核对两条虚拟路由；可选名称覆盖只能与这些已固定端点匹配。缺少或变化的端点、同方向重名、物理端点和不受支持的虚拟端点会在 Host 启动前失败关闭，不会选择 Windows 默认设备或其他麦克风。该解析通过共同的 supervisor 服务于 NapCat 和 SnowLuma；自动发现代码和离线测试不构成真实通话验收。`peer_map` 只绑定经 OneBot 身份响应核对的 UID 与 QQ 号，不是准入名单；来电准入继续由所选 NapCat/Snow adapter 的现有 allow/deny lists 决定。

## 当前行为与验收边界

### 启动与可选音频依赖

启动仍以 `NachoBot/plugins/qqcall_plugin/config.toml` 为唯一公开配置、以 `[plugin].enabled` 为唯一 QQCall 开关；示例配置仍默认禁用，已有 canonical 配置应读取和编辑而不是被示例覆盖。三个根启动批次 `launchbot.bat`、`launchbot_lite.bat`、`launchbot_potato.bat` 以及 `NachoBot-SnowLuma-Adapter/run.bat` 都通过 `adapter_extra.py` 根据 canonical 配置选择可选的 `qqcall-audio` extra，再以 `uv run --no-sync` 启动所选 adapter。WebUI 为匹配且启用的 adapter 加入 `--extra qqcall-audio`。在所选 adapter 项目中手动运行 `uv sync --extra qqcall-audio --locked` 仍然有效；本段不要求改动配置或增加任何 peer/准入项。

`qqcall-audio` 提供 QQCall 本地 PCM 传输边界；VAD/ASR 由 UniversalVC pipeline 处理，Core 沿用既有语音选模流程，不存在 QQCall 专用模型切换分支。NapCat.Shell 启动器和二进制仍是 NapCat、SnowLuma 共用的 Host 前置依赖。启用音频还要求 hash 校验的 native guard `.node`/`.dll`、私有 guard 配置和仍唯一、活动、可识别的虚拟 endpoint；缺失、热插拔变化或身份不符都会失败关闭，不回退 Windows 默认设备。部署还需匹配固定 SHA-256 的 `QQCallRecoveryShim2.dll`。

私聊自动接听、群指令/语义加入和被邀请加入都检查当前必需链路：Core 可用、TTS 可实际合成、按 Core 当前模式必需的 ASR/感知服务就绪，以及 UniversalVC 的实例身份、协议、pipeline/VAD/说话人能力就绪。检查使用现有配置和认证信息；任一项失败、超时或返回无效健康数据都拒绝接听/加入，并一次汇总已检测到的全部缺失项，例如“暂不能接听或加入通话：TTS、UniversalVC链路缺失或未就绪，请恢复后重新拨打/邀请。”模型已加载或端口在监听不等于链路就绪。现有白黑名单、忙线和房间检查继续生效。

### 群语音准入与验收边界


Core 的群加入指令是 `#join_vc_qq`，只接受最近 120 秒内身份匹配的实时群成员消息。原先的 `请加入群语音`、`加入当前群通话`、`加入群语音` 已取消指令注册，文字及语音转写均按普通消息处理；目前没有开放式自然语言加入入口。所选adapter/native仍需确认其白黑名单、该群已有活动语音房间，以及机器人没有被其他通话占用；Core 不判断房间是否真实存在。空闲时，Control API 从 Bridge 查询新鲜的原生房间成员信息，作为加入前的房间存活证明；查询不建立通话或音频会话。已有全局通话时返回 busy，查询过程中出现占用或连接变化也不能留下正面的准入证明。实际加入仍须收到与当前群和请求关联的 `20004` 成功回调。



群音频启动与后台刷新共享一个串行设备观察器，刷新间隔为 50 毫秒，设备证明有效期仍为两秒。原生退出的控制响应会给已经发起的 ACK/新鲜成员查询一个短暂的完成窗口；只有原生驱动确认机器人缺席才释放通话占用，等待超时不会将 ACK 当作释放证明。这些修复的针对性测试通过，新的刷新节奏和退出响应仍须完成实机回归。

群音频接入组件使用 UniversalVC 公开 API，将其匿名说话人标注和 ASR 回执投递到原有 QQ 群上下文，走 Core 已有的 Replyer 直连入口。说话人标签不会转换为 QQ 用户身份；触发加入的真实用户仅用于适配器名单校验。每个语音片段及回复调度都须重新核对当前房间、连接代次和已接受的 UniversalVC 片段；共享协议与 Core 消息构造不依赖安装音频包。

这些组件已有原生群房间租约与音频启动的生产接入代码，群 PCM 捕获及群 TTS 回传仍需实测。没有当前原生绑定时，`group_audio_status` 返回未激活；字段格式正确、房间查询响应或测试中的模拟绑定都不能启用真实群音频。`qqcall_group` 发送目标也必须由共享 QQCall 运行时消费：没有存活音频绑定时返回 `group_audio_unavailable`，格式无效时返回 `stale_group_scope`，不会转成普通群消息或调用私聊音频后端。组件测试通过不代表多说话人日志或声音回传已完成实机验收。

### 私有通话结束与普通消息

结束或拒接仍受私有音频目标、已核对来电关联、允许阶段和私有范围守卫限制。hangup 发送 QRTC cmd 8；相关正 ACK 后才会预留并通过 Host 守卫发送 cmd 10，两个 ACK 都不是终态，仍需相关终止事件确认结束。reject 发送 cmd 9，但 ACK 仍不确认拒接完成；真实原生 reject 尚未验证。发送结果不确定或 Host 丢失时会保留未确认状态与当前通话占用，不把音频停止单独当作挂断。




旧探针和独立 Bridge 操作说明从下面开始，保留用于历史诊断，不是当前常驻服务的启动流程。

## Historical probe implementation

本节记录早期独立探针，用来验证 QQ Node 环境读取已有 wrapper Session、访问 AVSDK Service、注册本探针自己的 Kernel listener，并通过本机命名管道交换受限消息。它不依赖 NapCat 或 SnowLuma API；旧结论不代表当前 adapter 共存状态。

启动时 Agent 先完成带每次运行随机令牌的 IPC 握手，再做只读能力检查。它只调用现有的 `NodeIQQNTWrapperSession.getNTWrapperSession(sessionName)` 和该 Session 的 `getAVSDKService()`，并在对象提供 `isNull()` 时检查原生空句柄；不会创建、初始化或登录 Session。能力检查不会注册 listener；Broker 收到显式 `attach` 命令后才注册。Bootstrap 只有在 `wrapper.node` SHA-256 命中已核实版本时才允许 attach，并使用该版本的 listener 对象移除契约；未知版本会返回 `listener_removal_contract_unverified` 且不会注册 listener。本节记录的历史独立探针没有自动接听、接听策略或通话状态机。

## 原生构建

可单独编译原生 Shim：

```powershell
powershell -ExecutionPolicy Bypass -File .\native\build.ps1
```

上方命令生成历史探针 `QQCallShim.dll`。正式 supervisor 使用以下 Recovery2 产物：

```powershell
powershell -ExecutionPolicy Bypass -File .\native\build.ps1 -RecoveryAttempt -PythonExe ..\NachoBot\.venv\Scripts\python.exe
```

正式构建使用 `native/recovery-shim-release.json` 的已验证发行信息和 MSVC 14.44.35207。在独立候选目录编译后，仅将该发行版本的两处链接时间戳固定到原发行值，并要求整文件 SHA-256 与 supervisor 原固定摘要完全一致；代码、格式或工具链产生其他差异时拒绝发布，不更改现用 DLL。已有相同摘要的产物保留原文件。编译输出位于 `native\build\x64\`，临时候选留在其 `candidate-*` 子目录；编译不等于在 QQ 中加载或运行。绑定继续仅使用 `start-qqcall.bat`。

## Broker 与配置

先启动 Broker；`--pid` 是目标 QQ PID，`--app-path` 是该 QQ 进程对应的应用目录。`--bootstrap` 指向本仓库的 `agent\bootstrap.cjs`；可选 `--wrapper` 仅在 wrapper 不在当前 Node require cache 时，指向 QQ 应用目录内已发现的 `wrapper.node`：

```powershell
$qqPid = 12345
$qqAppPath = 'C:\Path\To\QQ\resources\app'
$reportBase = Join-Path $PWD "NachoBot-QQCall\probe\.runtime\qq-$qqPid"
node .\NachoBot-QQCall\probe\broker.cjs `
  --pid $qqPid `
  --bootstrap (Resolve-Path .\NachoBot-QQCall\agent\bootstrap.cjs).Path `
  --app-path $qqAppPath `
  --session nt_1 `
  --generation 1 `
  --report $reportBase
```

Broker 会生成 `\\.\pipe\nachobot-qqcall-<QQ-PID>-<nonce>`，并在 `probe\.runtime\` 写入供 Shim 读取的私有 JSON 配置。配置含 IPC 令牌，不要打印、提交或分享该文件。Agent 的脱敏状态写入 `<reportBase>.agent.json`；原生 Shim 状态写入 `<reportBase>.shim.json`；Shim 延迟加载诊断写入 `<reportBase>.bootstrap.json`。这些运行状态文件也应留在本地。

Broker 在当前终端接受以下命令：

```text
status
attach
detach
quit
```

`status` 展示脱敏的 bootstrap、Session、Service、listener、wrapper hash gate 和运行时版本状态。`attach` 才注册本 Agent 的 listener；只有 pinned wrapper build 才允许该操作，并由 Agent 把同一个 listener 对象传给 `removeKernelAVSDKListener`。`detach` 表示移除 API 调用正常返回，不代表已观察到 vendor 内部状态变化。`quit` 会先要求确认 detach，再关闭 Broker。端点默认十分钟后关闭；需要更长的人工验证时，可显式传 `--lifetime-ms <1000..3600000>`，最长一小时，仍是有界探针，不代表常驻服务。若 QQ 端失联且无法确认清理，状态会保留为未确认，不会伪报成功。报告可能包括 `<reportBase>.agent.json`、`<reportBase>.shim.json`、`<reportBase>.bootstrap.json` 和固定恢复探针的 `<reportBase>-recovery.recovery.json`；都只记录脱敏状态。

注入器的发现、dry-run、注入和撤销命令由 `scripts\qqcall_injector.py` 提供。发现与 dry-run 不修改目标进程；`--inject` 会加载 Shim 并改变目标进程，`--cancel` 在仍可撤销时恢复本 Shim 的 IAT 槽，也会写入目标进程。注入必须使用刚发现的 QQ PID 与对应 `createdUtc`，并在原生隔离测试和代码审查通过后由主会话按当前授权执行：

```powershell
python .\NachoBot-QQCall\scripts\qqcall_injector.py `
  --inject `
  --pid $qqPid `
  --expected-start $qqCreatedUtc `
  --config $brokerConfigPath
```


## IPC 与隐私边界

Agent 是连接本地 Broker 的命名管道客户端，QQ 进程不开放入站网络服务。每条 JSON 行都带 schema 1、单调递增序号、连接 generation、requestId 和每次运行生成的认证令牌；错误 generation、令牌、序号、JSON 或超过 1 MiB 的帧会拒绝。发送队列也有固定上限。每个 Broker 只接受一个已认证连接；断线后应创建新 Broker 并使用新的 generation 与令牌，旧 Agent 不能重用已消费的 generation。

Kernel 回调数据只通过认证的本机 IPC 转发，Buffer/Uint8Array 编码为 Base64。日志、Broker 事件摘要和状态文件仅记录方法、类型、长度和能力阶段，不记录 UID、UIN、登录参数、令牌或原始事件内容。20001 输出仅接受 `[number, string]`，并调用已验证的 `setActionFromAVSDK(number, string)`。cmd 1 登录和 cmd 5 接听请求都使用正的安全整数 request ID；ID 为 0 的回调不能伪装成这两种命令的 ACK。

## 独立 Bridge 与共享 AV Host mock

`bridge\qqcall-bridge.cjs` 提供显式控制的单通话状态机，`bridge\host-client.cjs` 通过带 Bearer 鉴权的 loopback HTTP 与独立 Host 通信。它们复用 Broker/Kernel Agent 和 Host Transport，不调用 NapCat 或 SnowLuma API。早期 mock 阶段没有启动机器人、QQ 或 AV Host，也没有注册真实 listener、登录或接听；当时 Host/Bridge 的联通证据来自本机 mock HTTP 与 Windows named-pipe 集成测试。Mock and synthetic checks do not establish real QQ runtime acceptance.

Bridge 启动后保持 capability/status-only；CLI 不会自动 attach、login 或接听。所有操作由当前终端明确输入。`--token-file` 可指向纯文本私有 token 文件，或包含 `ipcToken` 的 Host private sidecar/原生配置 JSON；CLI 不显示 token。它生成供 Shim loader 使用的 Broker 配置，但不会调用注入器。运行前，Host 已由独立的受控流程启动，并须使用相同 token、Host 输入端口、回调端口和 `host-generation`：

```powershell
$qqPid = 12345
$qqAppPath = 'C:\Path\To\QQ\resources\app'
$hostSidecar = 'C:\Private\qqcall-host-private.json'
$configPath = Join-Path $PWD "NachoBot-QQCall\probe\.runtime\agent-$qqPid.config.json"
node .\NachoBot-QQCall\bridge\cli.cjs `
  --pid $qqPid `
  --app-path $qqAppPath `
  --wrapper (Join-Path $qqAppPath 'wrapper.node') `
  --session nt_1 `
  --token-file $hostSidecar `
  --host-port 6111 `
  --output-port 6110 `
  --host-generation 1 `
  --connection-generation 1 `
  --config $configPath
```

该命令只启动本地 named-pipe Broker、Host 回调客户端和终端控制器，并写出私有 bootstrap 配置。当前已观察到的目标应用布局将 `wrapper.node` 放在所选 `appPath` 根目录，应以目标 QQ 进程实际的 `--app-path` 为准；若它已在 QQ Node require cache 中，也可以省略 `--wrapper` 让 Agent 从缓存发现。若之后由获准的外部流程加载 Shim，Bridge 命令为：

```text
status
attach
login <privateIdentityFile>
accept <opaqueCallId>
quit
```

身份文件是私有 JSON，至少含 `selfUid`、`selfUin`、`accountPath` 三个字符串。CLI 不回显文件内容、身份值、原始 callback payload、token 或未知命令文本。`quit` 仅在 Agent 明确确认移除本 listener 后报告 clean stop；detach 未确认时会显示安全错误码，并允许用户再次尝试。若 stdin EOF，CLI 会串行尝试同一 clean shutdown；失败时明确报告未确认，然后关闭本地控制资源。

Bridge 的状态 ID 是本地生成的不透明 `callId`。`cmd=1` 和 `cmd=5` 的 posted receipt 只表示 Host 成功调用了 `postMessage`；登录或接听阶段仍要等待与当前正整数 request ID 和 generation 匹配的 AVSDK ACK。ACK 先于 HTTP receipt 到达也能处理。非音频路径的普通 `20004` ACK 按正 request ID 关联。显式配置 `audioDeviceTarget` 时，只接受完整的 `20004,id=0` 七项入房事件，并要求当前 pending accept、Host/Kernel/call generation、`scene === invite[0]`、`peerUid === 已核对的 Kernel peer UID === invite[1]` 及类型校验通过。实机确认 20004 的 `relation_id` 与 20006 的邀请 relation 不同，不能要求它等于 `invite[4]`；该入房关系标识仅保存于私有 call state。零 ID 事件可先到并被暂存，但只有正 ID 的 cmd 5 成功 ACK 到达后才转为 connected；它本身不是 cmd 5 ACK，音频路径也不能用泛型正 ID 的 20004 ACK 绕过这组关联条件。

20006 回调至少需要 12 个字段，并映射到经源代码核对的 accept tuple。若 Kernel invite 含非空 `from_uid`，它必须与回调中的 caller UID 一致，关联来源为 `kernel_from_uid_matched`。当前 Windows 实机也观察到 `from_uid` 为空、`relation_id` 为主叫 QQ 号的邀请；仅在显式提供已核对的 QQ 号→UID 映射时，Bridge 才允许以该 QQ 号解析 peer UID，并再次与 20006 精确匹配，关联来源为 `kernel_relation_uid_matched`。非空原生 UID 不会被映射覆盖，错误类型也不会回退。未配置或未命中的邀请仍不满足音频身份条件。Bridge 不自动接听；操作者必须从 `status` 读取当前 `callId` 后输入 `accept <callId>`。`cmd=55` 信令等待成功的登录 ACK，但 20001 使用独立、有界、按序转发队列，不会被登录等待阻塞。邀请重复不会重置计时；另一个活跃 invite 返回 busy。

CLI 可选参数 `--kernel-peer-identities-path <privateJSON>` 必须与 Adapter Control 参数一起使用。文件精确包含 `accountUin`、`targetPid`、`connectionGeneration`、`peerUidsByUin` 四个字段；前三项必须匹配当前机器人账号、Kernel PID 与连接 generation。映射最多 64 项，QQ 号必须为规范十进制字符串，UID 必须符合当前已观测的 24 字符 `u_` 格式。操作者应从当前登录账号的独立身份查询核对映射后生成此私有文件，不能将未核对的 AVSDK 回调直接当作映射来源。Core、Control 和音频 Runtime 仅信任上述两个精确来源枚举，并继续执行 UID→QQ 号白名单、lease 和 call scope 校验。

显式配置 `audioDeviceTarget` 时，原生 `20011,id=0` 的 `OnRoomDestroy` 必须是 `[scene:int32, relation_id:string, room_id:string]` 三项元组。只有当前 call 已保存通过校验的 20004 入房关系标识，20011 的 scene 与 relation_id 又精确匹配该 call，并通过当前 Host、Kernel 和 call scope 检查时，才可结束会话。20004 与 20011 的关系标识相等尚未实机验收；不匹配时保持拒绝，入房之前的 20011 不清空会话。房间 ID 仅检查字符串类型和长度，不作为额外的关联证据。确认后先向 Control journal 投影 `ended` 终态，再释放 slot；轮询的 `currentCall` 随后为 null，音频 Runtime 停止旧会话。状态记录 `roomDestroyedConfirmed:true`、`remoteEndedConfirmed:false`，不推断谁挂断。

同一 controller/Host generation 保存最多 64 个已匹配的 20004 入房关系标识，当前事件重复不重复入账。同一主叫、scene 和邀请 relation 可发起下一通，但必须取得新的入房关系标识；复用旧入房标识或达到上限时要求换新的 Host 和 controller，容量检查在设备选择和 cmd 5 之前执行。不确定、超时或尚未关联入房的终态仍保留会话槽，要求新 Host。历史不会因同一 Host 的断连、可用性变化或重新 attach 清空。换 controller 也必须换 Host，不能靠重建 controller 绕过历史保护。迟到的设备选择结果、cmd 5 ACK、20004 或旧房间销毁回调不能恢复已经结束的通话。未启用音频设备目标时，保留原有生命周期行为。

固定版本 DLL 的静态分析和实际加载模块的只读分发表检查已定位 cmd 9 Reject、cmd 10 Close 及 `[scene, peer_uid, reason]` 参数形状；在本节记录的旧版本中，reason 枚举和正常挂断命令当时仍未验证，因此 `hangup`/`reject` 不发送原生命令，Control API 返回 HTTP 501 和 `unsupported_unverified_command`。音频模式新增了独立的保守 Windows Kernel 结束关联：只接受 args 恰为两个元素、`destroyReason === 1`，且单项 `pushRoomInfoList` 的 `roomId` 与当前已关联的 20004 `enterRelationId` 完全相同、`peerUid` 与已核对来电者相同的事件；还要求当前已连接 call、有效 generation 与已登记且未复用的入房关系。当前实现按捕获形状要求 `termAction`、`termType`、`bussinessType` 均为数值 0，`peerUin` 为单字符字符串。这只是对观察到的事件形状作精确匹配，不声明 `destroyReason` 的通用枚举含义；Linux 风格的 relation-only 观察仍保留为 `end_observed`，不能清除会话。真实挂断和通话结束路径仍须在目标环境单独验收。Host 崩溃、断连和 timeout 也不代表远端已挂断。

Host 的 POST /v1/audio/devices 是独立的只读设备查询入口，只在 Electron 从实际解析的 Windows 插件文件计算出已固定 AVSDK SHA-256 时开放。它固定发送 cmd 103 和空参数，响应只确认 PPAPI postMessage 已调用，不代表设备列表已返回或音频路由已选择；其他平台或插件摘要返回 audio_device_protocol_unverified，通用 /v1/invoke 仍拒绝 cmd 103。此请求不会启动音频或更改设备选择。

同一摘要验证也保护 POST `/v1/audio/select`。它接受精确的输入/输出设备名称，先发送只读 cmd 103 并等待新鲜 20061 设备快照，再按该次枚举的 1-based ID 发送 cmd 102 `[inputId, outputId]`，最后重新查询并要求 currentOutput/currentInput 精确匹配且 ID、名称、GUID 映射和默认值保持不变。只有明确选择的正 ID 才算成功；0 代表默认设备，不会作为已选目标。响应只返回 `{selected:true,id,command:102}`，不泄露枚举或 GUID；它证明 SDK 回读确认了选择，不证明端点正在传输 PCM。超时或回读不一致会失败关闭，不会自动重试；通用 `/v1/invoke` 仍拒绝 cmd 102 和 103。

旧语音实现曾在 Core 中动态加载 UniversalVC 内部 VAD/WAV 模块；这条实现已退役，当前 Core 只使用静态复制且带 provenance hash 的公开 HTTP/WebSocket SDK，由 UniversalVC pipeline 负责 VAD/ASR。`qqcall-audio` extra 仅安装 `NachoBot-QQCall` 下的 QQCall PCM 设备驱动，不包含 VAD/ASR/模型库。NapCat 或 SnowLuma 启用该可选驱动时需在相应 adapter 目录执行 `uv sync --extra qqcall-audio --locked`。当前这轮只用真实 loopback 服务与假设备覆盖协议和 drain/fence；尚未部署服务或完成新的 Windows 语音往返。

## 可选的本机适配器控制服务

Bridge CLI 默认不开放适配器控制端点。只有显式同时提供 `--control-port`、`--control-token-file` 和 `--account-uin` 才会启动服务；控制令牌必须是独立私密 token，并且不能与 Host token 相同。`--account-uin` 是操作者明确配置的规范数字 QQUIN（`^[1-9][0-9]{0,19}$`），终端显式执行 `login` 时会与身份文件的 `selfUin` 比对。机器人账号仍须由适配器核对已验证的 OneBot account；当前 Shim/native Session 没有提供账号反查证明，因此不能称为 native account verified，操作者仍须选择正确的机器人 QQ PID 和 Host identity。可将 `--control-port 6120 --control-token-file 'C:\Private\qqcall-control-token.json' --account-uin '10001'` 追加到上面的 CLI 命令。

服务只绑定 `127.0.0.1`，每条 HTTP 路由都要求 `Authorization: Bearer <control-token>`。接口为 `POST /v1/adapter/poll`、`POST /v1/adapter/release`、`POST /v1/call/command` 和可选的 `POST /v1/audio/route`。一个 client instance 持有默认十秒的活动 lease；释放 lease 不会停止 Bridge。命令还需匹配 service instance、Host generation、Kernel connection generation、call ID 和 call generation。`accept` 只有在相关 20006 回调可用后才会发送，成功响应只表示 AVSDK Host 的 `postMessage` 已被调用；本节所述旧 Bridge 版本中的挂断与拒绝保持明确的 `unsupported_unverified_command` 错误。

### 自动接听的有界私有范围证明

Core 的共享 `[qqcall.policy].auto_accept` 只允许运行策略检查，本身不代表来电是私聊。自动接听还要求本次来电带有当前原生 listener 产生的私有范围证明，并在发送命令前重新核对账号、OneBot 身份、准入、peer map、当前 Kernel PID/FILETIME、service/Host/connection/call generation、lease 与现有全局通话槽。策略或调用在异步检查期间变化时会拒绝接听；每个 call scope 最多提交一次自动 `accept`。缺少证明时状态使用 `private_scope_unverified`。

该证明只开放给当前已知 `wrapper.node` SHA-256 `29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2`，并且只将已静态追踪到的 C2C `invite_type=1` producer chain 作为候选私聊。Bootstrap 从实际加载或缓存发现路径取模块文件并校验其文件哈希；它不证明进程内映射字节的摘要。Agent 在原生 `OnInviteActionToAVSDK` listener 回调中读取自有数据字段 `relation_id`、`invite_type` 和 `from_uid`；Broker 再绑定当前已确认 listener 注册、单调 IPC 序号、连接 generation、目标 PID，以及由 supervisor 的当前 `Evaluation.kernel_filetime` 经 CLI 传入的 FILETIME。旧 CLI 调用缺少 FILETIME 时仍兼容，但不能产生这个证明。身份映射还必须由 Bridge 与同一邀请的 20006 UID 精确关联；空 `from_uid` 只在当前 `relation_id` 是映射到该 peer UID 的规范 QQ 号时成立。已证明的忙线通知也使用各自邀请的证据，不复用占用中通话的证明；普通状态不包含 UID 或 relation ID。

其他 `invite_type`（包括 2、3）、未分类或旧协议事件不会获得私聊证明，因此不能据此自动接听或生成私聊忙线通知；群加入触发与现有 LLM/准入策略不变。静态链限于该 on-disk wrapper build 的已知 producer，不是全 binary 穷举、live mapping attestation 或真实生产来电验收。type 2/3 语义仍未知，当前没有真实来电捕获证明实际生产邀请使用 type 1；准确范围与地址记录见 [`native/qq-9.9.26-44343-avsdk-binding.md`](native/qq-9.9.26-44343-avsdk-binding.md)。

需要为持续音频检查 Host endpoint session 时，可以额外指定 `--audio-route-config <private-schema-1-json>`。此选项依赖上面的三个 control 参数，并要求 `--token-file` 使用 Host 私有 JSON sidecar，其中 `ipcToken`、`targetPid` 和 `hostGeneration` 必须与音频配置一致；裸 token 文件无法证明 Host 身份。音频配置列出 `hostPid`、`hostGeneration`、绝对 Python `.exe` 路径、明确的 Host/child PID 与十进制 FILETIME 创建时间，以及精确的 `captureRenderName` 和 `playbackRenderName`。Host PID 是选择的 AVSDK Host 进程；CLI 的 `--pid` 仍是独立 Kernel Agent 目标，两者可以不同。配置此选项时，Bridge 在发送 cmd 5 接听前，会要求 Host 将 `playbackRenderName` 选为 SDK 输入、将 `captureRenderName` 选为 SDK 输出，并等待 SDK 回读确认；未配置时，原有信令调用保持不变。接听时 `currentCall.acceptState` 会显示 `selecting_devices`，选择失败会显示 `selection_failed`；Control Service stop 先使 lease 失效，选择返回后仍须同步复核 lease 和完整 call scope，过期请求不会发送 cmd 5。之后的 Windows session observer 也按相反方向检查 Host capture/render endpoint，不创建音频 session。可选 `discoverChildren:true` 会只读发现同一 pinned Host 的同 image 后代：核对父链、前后完整后代集合与 FILETIME；新增、消失、重建或身份变化会使该次证明失效。未启用时仅检查配置中的固定进程。

每次 `POST /v1/audio/route` 都要求当前账户、lease、完整 call scope 和 connected 状态在观察前后保持有效。Bridge 通过无 shell 的 `execFile` 调用仓库固定的 `audio/windows_routes.py`，读取显式进程的 Windows active-session 元数据；它只接受两个名称精确匹配、端点不同且各有唯一 active/single-process Host 子树 session 的结果。单次子进程观察最多两秒，成功证明最多缓存一秒；没有 observer、超时、共享 session、失活端点或身份变化都会返回 `verified:false`。这只证明观察时 Windows session 与已选 Host process/endpoint 的关联，不证明远端听到音频，也不触发登录、接听或音频播放。

事件 journal 仅保留登录、邀请、接听、通话和连接生命周期状态，普通诊断状态不会消耗事件 cursor；轮询的 `currentCall` 始终反映最新状态。回调完成关联前，调用只呈现为 `awaiting_20006`，不能据此接听。请求体限制为 16 KiB，服务限制并发请求和命令队列，错误响应只包含固定错误码，不回显请求内容或身份值。独立 CLI 探针模式的 Broker 默认十分钟，`--lifetime-ms` 最多可设为一小时；正式 supervisor 使用 `--service-mode`，不安装这个探针生命周期定时器，也不能同时设置 `--lifetime-ms`。这消除了探针的一小时强制退出限制，不代表已完成 24 小时运行验收。适配器暂时掉线/重启会保留健康原生绑定，暂停准入、撤销旧 lease 并按原 scope 请求结束通话；同一 QQ 进程/账号/身份映射/音频路由重新验证后恢复服务，不重复注入。显式停止服务仍解绑并重试清理所属进程。已消费的一次性 Shim 连接本身失效后，仍需新机器人 QQ 进程才能重建绑定，不能靠恢复逻辑或删除隔离标记复活。

## 独立 AVSDK Host early-loader

`scripts\launch-host.ps1` 使用仓库内已有的 `NapCatWinBootMain.exe` / `NapCatWinBootHook.dll` 开发加载器接口启动一个单独的 QQ Host 进程。命令要求显式给出 QQ 可执行文件、对应 `resources\app` 和 `NapCat.Shell` 根目录；它只读取 `NapCat.Shell\qqnt.json` 的版本元数据，随后在 `NachoBot-QQCall\.runtime` 下生成自己的兼容清单、早期加载脚本、private launch plan 和全新 userData profile。兼容清单保留源 `main`（目前为 `./loadNapCat.js`），自有 early-loader 通过 Hook 的 `NAPCAT_LOAD_PATH` 加载。它不会修改 vendor 的 `qqnt.json`、QQ 安装目录或 NapCat 文件。

Host early-loader 在 Electron `app` ready 前加载现有 `host\electron-main.cjs`，由它注册 `AVSDKPlugin.dll` 的 PPAPI 路径并启动本机 Host。启动配置在 QQ 进程内按真实 PID 创建；IPC token 只保存在 Host sidecar/bootstrap 私有文件，不放在 argv、环境变量、launch plan、终端或诊断状态里。`-DryRun` 会验证路径与兼容清单并写出可审阅的非秘密 plan/profile，不启动任何进程：

```powershell
$qqExe = 'C:\Program Files\Tencent\QQNT\QQ.exe'
$appPath = 'C:\Path\To\QQ\resources\app' # use the exact app path for the QQ version being launched
$napCatShell = Join-Path $PWD 'NapCat.Shell'
powershell -ExecutionPolicy Bypass -File .\NachoBot-QQCall\scripts\launch-host.ps1 `
  -QQExePath $qqExe `
  -AppPath $appPath `
  -NapCatShellRoot $napCatShell `
  -HostGeneration 17 `
  -DryRun
```

移除 `-DryRun` 才会通过 `Start-Process -WindowStyle Hidden` -WindowStyle Hidden 启动 QQ。Host 仍是 PPAPI/HTTP 层；它不注入 Kernel Shim、不读取账号身份、不注册 listener、不登录、不接听。Host 的 /healthz ready 只说明插件渲染器和 HTTP 通道可用；设备选择被 SDK 接受也不证明存在 PCM 路由、远端播放或完整通话。

Host 启动后，launcher JSON 会给出 launcher PID 和本次私有运行目录；BootMain 的 stdout/stderr 写入该目录的 `launcher.stdout.log` 与 `launcher.stderr.log`。early-loader 若开始执行，会写 `earlyloader.status.json`，只包含阶段、QQ 进程 PID、generation 和固定错误码，便于区分 Hook 是否命中与 Host main 是否启动。Bridge 可使用生成的 sidecar 作为 `--token-file`，并用 Host sidecar 中的目标 PID、配置端口和 generation 启动本机 CLI。Kernel Agent 连接后才可用新的 `identity` 命令从用户明确提供的私有 OneBot 身份 JSON 生成登录文件：

```text
identity <privateOneBotIdentityFile> <outputPrivateLoginFile>
login <outputPrivateLoginFile>
```

输入 JSON 必须提供 `selfUid` 以及规范数字字符串 `selfUin` 或 `accountUin`。Agent 先调用已有 Session 的 `getAccountPath(Number(uin))`；仅当该方法缺失或返回空值时，才调用只读 `NodeQQNTWrapperUtil.getNTUserDataInfoConfig()`，并仅接受已存在的绝对 Windows 目录。它不会创建或初始化 Session、猜测默认目录或登录。路径只通过本机认证 named-pipe RPC 写入用户指定的私有输出文件；CLI 不显示身份值、路径或原始身份文件。独立 early-loader 与 identity RPC 仍需在目标 QQ 版本上完成运行时验收。
