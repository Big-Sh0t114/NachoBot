# NachoBot 本地多模态运行时

本目录维护本地 ASR、VLM 与 TTS 的具体模型路由。平台适配器直接连接 NachoBot Core 的 `8000/ws`；TTS 对 Core 暴露一个统一的 `9880` HTTP 运行时，运行时进程在内部托管所选 GPT-SoVITS 或 VoxCPM 后端。

## 独立自动启动

本目录 `startup.toml` 统一维护 `components.core` / `components.tts` / `components.perception` 三个开关，默认均为 `true`。
`core = false` 时，桌宠进入无核心独立模式；本目录的 `launch_services.bat` 也直接跳过模型服务启动，不检查模型环境。
`core = true` 时，`launch_services.bat` 根据另外两个开关为服务分别打开控制台（此入口本身不启动 Core）。
`runtime.profile` 选择已有 `.venv`（gpu）或 `.venv-cpu`（cpu），不在启动时重装共享环境。
也可继续双击 Live2D 的 BAT：仓库级编排器先调用这个独立入口，然后启动桌宠。
端口、引擎和模型读取本目录既有 `configs/base.toml`、`perception.toml` 等配置。
正在加载的服务通过锁复用，已经监听的服务验证健康接口后复用；不会接管或停止其他入口启动的服务。
模型日志在各自控制台直接打印，关闭该控制台终止它托管的进程树。
由桌宠 BAT 拉起的服务绑定该次桌宠启动会话，桌宠正常退出、启动失败或启动终端异常结束时同步清理。
桌宠只复用启动前已运行的服务，不接管其退出管理；独立执行 `launch_services.bat` 拉起的服务也不随桌宠关闭。
启动确认表示进程已创建，首次加载/准备模型继续在服务控制台进行，实际可用性以健康接口为准。

## 边界与端口

| 组件 | 默认端口 | 职责 |
| --- | ---: | --- |
| NachoBot Core | `8000` | 平台消息总线、`/api/multimodal` 与运行档位降级 |
| 统一 TTS Runtime | `9880` | 公开 TTS 健康检查与合成入口；内部托管一个选定后端 |
| 本地多模态运行时 | `9874` | FULL 的 ASR、VLM 与兼容 API |

Core 调用的运行时接口包括：

- `GET /v1/capabilities`
- `POST /v1/perception`
- 统一 TTS Runtime 的 TTS 接口（`9880`）

9874 仅负责 ASR/VLM 感知，不承载 TTS。平台适配器的消息链路始终是 Adapter → Core `8000/ws`。

## 运行档位

| 档位 | Core 感知路由 | 启动服务 | 回复语音 |
| --- | --- | --- | --- |
| FULL | local 失败后 remote，最终文本降级 | 9880 统一 TTS Runtime、9874 perception | 仅显式 `tts_text` 字段合成 |
| LITE | remote，最终文本降级 | 9880 统一 TTS Runtime | 仅显式 `tts_text` 字段合成 |
| POTATO | remote，最终文本降级 | NachoBot Core + 所选 QQ 适配器；本目录不启动服务 | 预建平台媒体行为保持不变 |

Compose 只提供 FULL/LITE 的多模态服务；POTATO 是 Core 文本档位，根启动脚本仍会启动所选 QQ 适配器，但不会从本 compose 文件启动本地模型服务：

```powershell
docker compose --profile full up -d
docker compose --profile lite up -d
```

托管引擎默认跟随当前 `configs/base.toml` 的唯一启用项；仅在需要临时覆盖时设置 `NACHOBOT_TTS_ENGINE=gpt-sovits` 或 `voxcpm`。

## 配置

若 `configs/` 中缺少文件，从 `template_configs/` 复制并去掉 `_template`：

```text
base_template.toml         -> configs/base.toml
gpt-sovits_template.toml   -> configs/gpt-sovits.toml
vox_template.toml          -> configs/vox.toml
perception_template.toml   -> configs/perception.toml
```

`base.toml` 的 `[enabled_tts]` 选择 GPT-SoVITS 或 Vox；具体参数分别维护在 `gpt-sovits.toml` 与 `vox.toml`。`perception.toml` 维护 9874 监听地址、Florence-2 与 Sherpa-ONNX 配置。Core 不包含这些本地模型名。

可在 `[tts_ambient]` 开启 TTS 环境声。模板默认关闭；例如，可用以下设置试用 `desk` 预设：

```toml
[tts_ambient]
enabled = true
preset = "desk"
level_db = -38.0
events_per_minute = 3.0
source_file = ""
```

`room` 与 `desk` 是合成音效，不是真实录音；`level_db` 表示连续环境床相对 PCM16 满幅的参考 RMS 电平，数值越负越轻，`-90` 表示静音。也可将 `source_file` 指向本地 PCM16 mono/stereo WAV（最多 30 秒、8 MiB，采样率 8–96 kHz）；相对路径以 `base.toml` 所在目录解析，文件会按输出采样率重采样并平滑循环。启用后 WAV 和 PCM 流都会在本次音频时长内混合；流式输出逐块处理并保留跨块状态，不会在请求间持续播放。环境声会在一次合成过程中缓慢变化音量与音色；这些变化只作用于环境声，不调制人声。配置在运行时启动时读取，改动后需重启 TTS Runtime。

TTS Runtime 在进程启动时固定读取一次活动配置并选择唯一后端。切换 GPT-SoVITS/Vox 或修改模型配置后需要重启 TTS Runtime；请求期间不会重载或切换本地模型。

## PyTorch 运行时依赖

FULL 多模态环境的 GPU manifest 固定使用 PyTorch `2.13.0+cu126`、TorchVision `0.28.0+cu126` 与 TorchAudio `2.11.0+cu126`，通过 PyTorch 官方 CUDA 12.6 wheel index 安装。GPU 环境需要支持 CUDA 12.6 的 NVIDIA 驱动；CUDA 12.6 GA 对应的驱动版本为 Windows `560.76` 或更高、Linux `560.28.03` 或更高，驱动由主机提供，不会由 Python 依赖安装。CPU-only 环境可用 `pyproject.toml.cpu` 中对应的 `+cpu` 三件套与官方 CPU wheel index。不要把 CPU 与 GPU wheel 混装。版本组合参见 [PyTorch 官方安装矩阵](https://pytorch.org/get-started/previous-versions/) 与 [NVIDIA CUDA 12.6 驱动说明](https://docs.nvidia.com/cuda/archive/12.6.0/cuda-toolkit-release-notes/index.html)。

TorchAudio `2.11.0` 使用 PyTorch stable ABI，并支持 PyTorch `2.11` 及更高版本。已对 Windows/Linux、Python 3.11/3.12 的依赖解析与 wheel 可用性做 dry-run 验证；模型加载和实时推理仍需在部署环境验证。详见 [TorchAudio 官方兼容性说明](https://docs.pytorch.org/audio/stable/installation.html)。

FULL/LITE 启动器与 TTS 托管 VoxCPM/GPT-SoVITS 环境固定到 PyTorch `2.13.0` 与 TorchAudio `2.11.0`：GPU 使用官方 cu126 wheels，CPU-only 使用官方 CPU wheels；若后端 requirements 引入 TorchVision，则约束到匹配的 `0.28.0` wheel。托管运行时会将版本约束应用到后续后端 requirements 安装，并在写入或复用就绪标记前检查 PyTorch/TorchAudio 版本；旧依赖标记会触发重新安装。Windows GPU VoxCPM `torch.compile` 使用 `triton-windows==3.7.1.post27`，其 PyTorch `2.13` 支持范围见 [triton-windows 官方说明](https://github.com/triton-lang/triton-windows)。这验证的是安装约束和包元数据；后端模型加载、编译及实时推理仍需在部署环境验证。

所有本地模型都在各自服务公开就绪前加载：FULL 的 9874 会完成 ASR 与 VLM 预加载；9880 会先启动并验证所选 TTS 后端，Vox 启用情感分类时还会在公开端口监听前加载分类器。任一必需模型加载失败，所属服务启动失败并由 Core 执行既定降级。

## 启动

推荐从仓库根目录启动：

- `launchbot.bat`：FULL
- `launchbot_lite.bat`：LITE
- `launchbot_potato.bat`：POTATO（Core 文本 profile + 所选 QQ 适配器；不启动本目录服务）

手动启动统一 TTS Runtime 与 FULL perception：

```powershell
uv sync --locked
uv run python scripts/container_tts_entrypoint.py --host 127.0.0.1 --port 9880
uv run python -m nachobot_multimodal.api_server
```

`container_tts_entrypoint.py` 按活动配置选择后端并把公开监听固定在 9880；FULL 另启动 9874 perception API。Core 通过 `NACHOBOT_TTS_ENDPOINT=http://127.0.0.1:9880` 与 `NACHOBOT_MULTIMODAL_ENDPOINT=http://127.0.0.1:9874` 访问它们。POTATO 不设置或访问这些本地模型服务。

也可直接使用内部托管管理器：

```powershell
uv run python scripts/tts_runtime_manager.py serve --engine gpt-sovits --port 9880
# 或
uv run python scripts/tts_runtime_manager.py serve --engine voxcpm --port 9880
```

## Docker

```bash
docker compose --profile full up -d
# 或：--profile lite
```

Compose 不设隐式默认档位，具体服务拓扑见上方“运行档位”。Core 与各平台适配器使用 `8000/ws`；FULL/LITE 的 Core 通过 `multimodal-tts-runtime:9880` 和（仅 FULL）`multimodal-perception:9874` 访问本地模型服务。

## 模型下载

模型优先复用 `models/` 与 `models/hf_cache/`。可用 `NACHOBOT_HF_ENDPOINT` 或标准 `HF_ENDPOINT` 指定 Hugging Face 镜像；默认禁用 Xet，并保留 Florence-2、VoxCPM、Sherpa-ONNX 与 GPT-SoVITS 现有的本地缓存和回退下载策略。
