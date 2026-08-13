# NachoBot 多模态适配器

NachoBot 的 TTS、情感预设、流式 ASR 与 VLM 服务。自 v1.0.0-pre-C 起，GPT-SoVITS 与 VoxCPM 推理运行时由本目录统一托管，不再依赖用户预先安装的外部客户端。

## 组件与端口

| 组件 | 默认端口 | 说明 |
| --- | ---: | --- |
| Multimodal Adapter | `8070` | 平台消息中继、TTS 路由、`/api/tts`、`/api/emotion_preset`、`/api/health` |
| 托管 TTS Runtime | `9880` | GPT-SoVITS 或 VoxCPM，二选一 |
| Perception API | `9874` | Florence-2 VLM 与共享 Sherpa-ONNX 流式 ASR |

主要能力：

- GPT-SoVITS：角色权重、参考音频、语言与生成参数。
- VoxCPM：参考音频/Voice Design、LoRA、切句与情感预设。
- 情感分类：使用多语言 NLI 模型把文本映射到声音预设，低置信度回退默认音色。
- VLM：OpenAI 兼容的 `/v1/chat/completions`；Florence-2 固定执行详细描述任务。
- ASR：OpenAI 兼容的 `/v1/audio/transcriptions`，并向 Bilibili、DiscordVC、UniversalVC 提供共享的逐块流式识别实现。

## 快速开始

推荐直接在仓库根目录选择启动档位：

| 脚本 | 启动内容 |
| --- | --- |
| `launchbot.bat` | TTS + Adapter + VLM/ASR |
| `launchbot_lite.bat` | TTS + Adapter，不启动 VLM/ASR |
| `launchbot_potato.bat` | 仅 8070 消息中继，不加载本地模型 |

脚本会优先复用 `models/` 与 `models/hf_cache/` 中的本地内容，缺失时再下载。默认可通过 `NACHOBOT_HF_ENDPOINT` 指定 Hugging Face 端点；根启动脚本默认使用 `https://hf-mirror.com`。

要求 Python 3.11 或 3.12。首次运行需要下载较大的模型和运行时，请观察 `logs/boot_setup.log` 与对应终端。

## 配置

若 `configs/` 中缺少文件，从 `template_configs/` 复制并去掉 `_template`：

```text
base_template.toml         -> configs/base.toml
gpt-sovits_template.toml   -> configs/gpt-sovits.toml
vox_template.toml          -> configs/vox.toml
perception_template.toml   -> configs/perception.toml
```

### `base.toml`

```toml
[server]
host = "127.0.0.1"
port = 8070

[enabled_tts]
enabled = ["Vox"] # "GPT_Sovits" 或 "Vox"，只启用一个

[tts_base_config]
stream_mode = false
post_process = false
```

`[routes]` 指向 Core 的 WebSocket 路由；默认 Core 为 `127.0.0.1:8000`。

### TTS 引擎配置

- `gpt-sovits.toml`：模型权重、参考音频、提示文本、语言和采样参数。
- `vox.toml`：`model_dir` 留空时托管运行时下载 `openbmb/VoxCPM2`；可选 LoRA、声音描述、参考音频与情感映射。
- 两份配置中的 `host` / `port` 是托管 TTS API 地址，通常保持 `127.0.0.1:9880`。
- VoxCPM 已移除旧的 `denoise` 字段。

### `perception.toml`

```toml
[perception]
host = "127.0.0.1"
port = 9874

[perception.device]
vlm = "cuda:0"

[asr]
provider = "cpu"
num_threads = 4
models_dir = "models"
auto_download = true
```

ASR 模型和配置只在这里维护；其他适配器不再保存重复副本。

## 手动启动

先同步依赖：

```bash
uv sync --locked
```

根据 `base.toml` 选择一个 TTS Runtime：

```bash
uv run python scripts/tts_runtime_manager.py serve --engine gpt-sovits --port 9880
# 或
uv run python scripts/tts_runtime_manager.py serve --engine voxcpm --port 9880
```

另开终端启动 Adapter 与感知服务：

```bash
uv run python main.py
uv run python -m nachobot_multimodal.api_server
```

纯中继模式：

```bash
uv run python main.py --no-local-models
```

该模式的 `/api/health` 返回 `relay_only`，不会启动 TTS、VLM 或 ASR。

## 目录要点

```text
scripts/tts_runtime_manager.py   托管并修补 TTS 运行时
src/tts/                         TTS 后端与统一路由
src/asr/                         共享流式 ASR 与模型管理
src/vlm/                         Florence-2 VLM
src/api_server.py                Perception API
nachobot_multimodal/             对外稳定导入命名空间
template_configs/                可复制的默认配置
models/                          模型、托管运行时与缓存
```

## Docker

```bash
docker network create nacho_bot
docker compose up -d
```

当前 Compose 默认只启动 `8070` Multimodal Adapter，并挂载 `configs`、`models` 与 `logs`；不会替你额外启动 `9880` 托管 TTS Runtime 或 `9874` Perception API。需要完整本地模型链路时，建议在宿主机使用根启动脚本，或自行把另外两个进程编排进部署环境。

构建使用 Python 3.12，并通过 `additional_contexts` 读取相邻的 `NachoBot/ncnk_message`。容器连接 Core 或外部推理进程时应使用容器可达的服务名/宿主机地址，而不是指向容器自身的 `127.0.0.1`。
