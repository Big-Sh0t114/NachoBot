# QQCall Windows 部署与验证

QQCall 的正式服务与 PCM 包应放在同一工作区的 `NachoBot-QQCall`，与 `NachoBot`、所选 QQ 适配器、`NachoBot-UniversalVC-Adapter` 和共享 Host loader 并列。不要复制开发机的 `.runtime`、登录数据、模型或私有 token 文件作为安装包。

## 依赖与编译

自动发现使用 `[qqcall].adapter = "auto"`，NapCat 优先，SnowLuma 作为另一候选。两类适配器均可发布各自的身份心跳，supervisor 只向完整核验通过的进程发布通话租约所需的绑定。自动模式从 `loader_root/config/onebot11_<机器人账号>.json` 读取 NapCat 已启用且在监听的本机正向接口；SnowLuma 使用其适配器配置和现有认证核验。不要为了自动发现新增无认证监听或修改其他账号配置。已注入的活动实例不会因优先候选后来上线而被抢占；显式单适配器模式继续使用原配置约束。

当前实机核验的 QQNT 是 `9.9.26-44343`；QQ 启动程序本身的文件版本不能代替应用目录内实际构建版本。Kernel wrapper 与 AVSDK 的原有摘要检查仍生效，其他版本不能仅凭能够启动就算支持。

排查“群内已邀请但机器人没有加入”时，可向本机控制服务的 `POST /v1/kernel/diagnostics` 发送 `{"accountId":"机器人QQ号"}`，认证沿用 `.runtime/service/control.token.private`。该接口读取当前 Agent 的各类回调次数、是否映射为 AVSDK 信号及参数类型，不包含回调正文、账号映射或通话内容，不领取或续期适配器租约。返回的 `hostGeneration`、`connectionGeneration` 必须对应当前绑定；进程代次变化或 Agent 不可用会返回错误，不能把旧计数作为当前邀请证据。此诊断不能证明已经入房，实际入房仍须通话状态与真实拨号验证。

QQ 消息适配器临时断线时，控制服务会暂停准入并允许同一进程恢复。若绑定的适配器进程已退出，则清理当前控制连接和它拥有的 Host：音频观察器绑定的是原适配器的 PID 与创建时间，不能把新进程直接当作旧音频读取者继续使用。一次性 Shim 已消耗的 QQ 进程此时需要重新启动并登录，再运行 `start-qqcall.bat` 建立新代次；不能以旧服务的 `ready` 文件代替恢复成功。

仓库已跟踪适用于这个锁定构建的 Windows x64 预编译产物，克隆后即可取得：`native/build/x64/QQCallRecoveryShim2.dll`、`native/audio_route/build/QQCallAudioRouteGuard-v2.node`、`QQCallAudioRouteGuard-v2.dll` 和 `artifact-manifest.json`。MSVC 仅在需要从源码重建原生文件时才需要；预编译文件仍受下述摘要与 manifest 校验约束。

Core 的锁定 Python 环境提供 supervisor 所需的 `psutil`；所选适配器的 `qqcall-audio` extra 安装 PCM 包与 Windows PyAudioWPatch。以下命令从完整工作区根目录运行，按实际选用的适配器二选一：

```powershell
uv sync --project .\NachoBot --locked
uv sync --project .\NachoBot-SnowLuma-Adapter --locked --extra qqcall-audio
# 使用 NapCat 时替换上行：
uv sync --project .\NachoBot-Napcat-Adapter --locked --extra qqcall-audio
```

共享 Host loader 需要 `NapCatWinBootMain.exe`、`NapCatWinBootHook.dll`、`qqnt.json`，即使消息适配器选 SnowLuma，也需要这些独立 Host 启动依赖。它们不是另一个必须启用的消息机器人。UniversalVC、ASR/VLM 和 TTS 使用各自现有部署，QQCall 不自行复制或更换模型。

### 可选：从源码重建原生文件

正常部署直接使用仓库预置文件。只有要从源码重建时才需要 MSVC `14.44.35207` 和 guard SDK。Recovery2 构建在候选目录完成，验证整文件摘要后才发布；已有相同摘要的 DLL 保留原文件。

```powershell
powershell -ExecutionPolicy Bypass -File .\NachoBot-QQCall\native\build.ps1 -RecoveryAttempt -PythonExe .\NachoBot\.venv\Scripts\python.exe
```

产物是 `NachoBot-QQCall/native/build/x64/QQCallRecoveryShim2.dll`，固定摘要与 `native/recovery-shim-release.json`、supervisor 的原校验一致。默认不带 `-RecoveryAttempt` 的构建只生成历史探针，不能代替正式产物。源码、工具链或格式产生其他差异时，构建失败；不要修改摘要来绕过失败。

重建 audio-route guard 时，SDK 获取脚本固定 Microsoft NuGet 的 [CPP 包](https://www.nuget.org/packages/Microsoft.Windows.SDK.CPP/10.0.26100.8249)及对应 x64 包，并校验 `native/sdk-release.json` 中来自官方 catalog 的大小与 SHA-512；只提取到工作区，不安装系统软件或模型。

```powershell
.\NachoBot\.venv\Scripts\python.exe .\NachoBot-QQCall\scripts\acquire-guard-sdk.py
powershell -ExecutionPolicy Bypass -File .\NachoBot-QQCall\native\build_audio_route.ps1
```

构建验证 CFG 元数据和指定导出，并生成公开的 `artifact-manifest.json`。

### 目标机本地音频准备

预编译 guard 不包含本机设备身份。实际音频仍要求配对的 `QQCallAudioRouteGuard-v2.node` / `.dll`、本机私有 guard 源配置，以及本机活动、唯一的虚拟端点。每台目标机都要用下列准备工具验证二进制摘要和活动端点；已有 guard 配置只验证、保留，损坏的已有文件不自动替换。公开 `guard_config_file` 为空时，runtime 视图解析为 `.runtime/service/audio-guard-source.private.json`，不回写 TOML；只有 `--apply` 才原子创建缺失文件。

```powershell
.\NachoBot\.venv\Scripts\python.exe .\NachoBot-QQCall\scripts\prepare-audio-route.py --list-devices
# 先查看干跑结果，再仅创建缺失的本机私有派生配置：
.\NachoBot\.venv\Scripts\python.exe .\NachoBot-QQCall\scripts\prepare-audio-route.py
.\NachoBot\.venv\Scripts\python.exe .\NachoBot-QQCall\scripts\prepare-audio-route.py --apply
```

虚拟路由不唯一时返回 `audio_route_selection_required`。用 `prepare-audio-route.py --list-devices` 读取虚拟设备友好名称和方向，在同一公开 TOML 的 `capture_device` / `playback_device` 中指定名称后再干跑。没有合法路由或端点在准备期间变化时拒绝写入；不选择 Windows 默认或物理设备，不复制其他机器的 endpoint ID。guard 技术边界仍见 [native audio route](../native/audio_route/README.md)。绑定继续仅用固定 BAT。

## 配置与入口

唯一公开配置是 `NachoBot/src/plugins/built_in/qqcall_plugin/config.toml`。已有文件从现有内容读取；只有不存在时才由内置插件从代码默认值生成。配置损坏应修正原文件，不能用模板悄悄替换。CLI 不负责创建不存在的 canonical 文件；新工作区应先完成 Core 插件配置准备。

| 配置 | 作用 |
| --- | --- |
| `bot_config.toml 的 [qqcall].enabled` | 唯一 QQCall 总开关，默认关闭。 |
| `qqcall.adapter` / `account_id` | 默认自动选择适配器；账号省略或留空时继承 bot_config 的 `[bot].qq_account`，显式账号不一致时拒绝启动。 |
| `qqcall.onebot` / `identity_file` | 当前适配器身份与鉴权；私有 token 留在私有文件。 |
| `qqcall.paths` | QQ 路径留空时从运行中的 QQ 进程发现；若存在多个安装路径则需显式指定。loader、Core Python 和 Node 使用仓库默认值，可手动覆盖。 |
| `qqcall.ports` | Host、callback、control 端口；与其他服务避免冲突。 |
| `qqcall.audio` | 可选设备名称覆盖；默认通过 runtime 解析和证明设备。 |
| `qqcall.universal_api` | 独立本机 UniversalVC API；优先复用已认证、就绪的共享实例。 |
| `qqcall.policy.auto_accept` | 自动接听策略，不能替代适配器名单或全局占用校验。 |
| `qqcall.native.guard_config_file` | 可选本机私有源路径；空值解析为受控默认路径，不能复用其他机器的设备身份。 |
| `qqcall.peer_map` | 经验证的身份映射，不是额外的来电允许名单。 |
| `qqcall.supervision` | supervisor 观察、重试间隔和同代失败上限。 |
| `allow_planner_accept` / `group_intent_*` | Core 候选及复审策略；沿用当前配置和选模，不跳过原生房间证明。 |

先启动 Core、所选普通 QQ 适配器，并登录机器人。WebUI 可选；固定入口直接复用或后台启动独立 supervisor，不依赖 WebUI API。绑定仅运行：

```powershell
.\NachoBot-QQCall\start-qqcall.bat
```

它等待当前代次、必要时申请一次 UAC、验证身份后绑定；ready 时不重复绑定。不要复用旧 `connection-N` 注入脚本。一次性 Shim 已消费并失联时，需要按正式停止流程重启机器人 QQ；保留主叫与共享模型服务，再用固定 BAT 绑定新代次。

适配器进程仍存活但连接暂时掉线时，supervisor 保留仍健康的原生绑定，进入 `waiting_adapter`；暂停控制和音频准入，并按原通话 scope 请求退出。同一适配器进程重新连接后，重新验证机器人 QQ 的 PID/FILETIME、账号/UID、身份映射与音频路由，通过后自动恢复服务能力，不重复 UAC 或注入。适配器进程退出则清理本代所属 Host，不能用新适配器进程复用旧音频观察器；一次性 Shim 已消耗时需要重启机器人 QQ。被中断的通话不保证继续，通话占用仍依赖原生退出证明释放。账号/QQ 进程/身份映射/路由变化和原生连接失效继续隔离；这项恢复不能复活已经退出的旧 Bridge。显式停止整个 QQCall 服务仍解绑并有界重试清理本代所属 Host 进程和启动器。

群通话音频已停止但原生退出尚未确认时，适配器保留隔离记录。后续轮询只有在同代房间和全局占用都确认释放后才清除记录；跨代恢复还要求 supervisor 身份清单校验通过、Host 与连接代次均递增，并确认新代全局占用空闲。正在使用的音频、未完成的停止操作、查询失败或迟到的旧代响应都不能清除占用。

也可在 WebUI 的 QQCall 卡片使用“注入”按钮。该按钮运行同一固定 BAT，沿用 UAC、账号/进程身份和单次 Shim 校验；已就绪时不会重新注入。外部启动的独立控制服务会被识别，WebUI 不将其接管为托管进程，通用启动请求也不会重复创建 supervisor。状态报告暂时过期时显示服务存活且等待状态更新，并禁用注入；需要重启 QQ 的状态不能直接重复注入。

只读检查使用 `scripts/qqcall-service.ps1 check` / `status`。`check` 成功只证明静态依赖和配置有效，不等于机器人已绑定、已入房或真实声音回传通过。

## 验收边界

源码构建、静态预检、配置检查和本机音频准备不能代替目标机上的真实账号绑定、通话、恢复和持续运行验证。完整安装验收需要在更新后的服务环境中完成这些运行时检查。
