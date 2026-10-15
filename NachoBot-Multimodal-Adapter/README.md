# NachoBot 本地多模态运行时

本目录维护本地 ASR、VLM 与 TTS 的具体模型路由。平台适配器直接连接 NachoBot Core 的 `8000/ws`；TTS 对 Core 暴露一个统一的 `9880` HTTP 运行时，运行时进程在内部托管所选 GPT-SoVITS 或 VoxCPM 后端。

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

TTS Runtime 在进程启动时固定读取一次活动配置并选择唯一后端。切换 GPT-SoVITS/Vox 或修改模型配置后需要重启 TTS Runtime；请求期间不会重载或切换本地模型。

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
