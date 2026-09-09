# NachoBot Universal Voice Adapter (Real-Time Edition)

基于 [ProcTap](https://github.com/m96-chan/ProcTap) 的 Windows 通用语音适配器。它从指定进程捕获音频，经 VAD、可选降噪、声纹追踪和共享流式 ASR 转成文本发送至 NachoBot Core，再把回复语音送入虚拟声卡，适合多人连麦与游戏场景。

## 🌟 核心特性

- 🎧 **进程级无损捕获** — 使用 ProcTap (WASAPI) 提取特定进程的原始 PCM 数据。
- 🔇 **智能批量去噪 (RNNoise)** — 内置基于 `pyrnnoise` 的深度学习降噪。VAD 触发后异步进行批量降噪，在保证低至毫秒级捕获延迟的同时，完美过滤游戏底噪、键盘声与环境噪音。
- 🎙 **高精度语音活动检测 (Silero VAD)** — 通过 `sherpa-onnx` 运行，比传统 RMS 阈值拥有更高的精准度，能完美切分连续对话。
- 👥 **实时声纹追踪 (WeSpeaker)** — 在多人频道中，能够通过声纹特征 (WeSpeaker ResNet34) 自动聚类并追踪说话人，给 NachoBot Core 提供说话人 ID 区分上下文！
- ⚡ **共享流式语音识别 (Zipformer)** — 复用 `NachoBot-Multimodal-Adapter` 的 2025 中文 xlarge INT8 模型，在 CPU 上逐块解码；VAD 结束时直接提交最终文本，无需重新识别整段语音。
- 📦 **单一模型所有权** — UniversalVC 只管理 Silero VAD 与 WeSpeaker；ASR 配置、实现和模型下载统一由 Multimodal-Adapter 负责。

## 💻 前置要求

1. **Windows 10/11** (20H1+)
2. **Python 3.11+**（推荐使用 `uv` 管理依赖）
3. **虚拟声卡驱动** — 推荐 [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) 或 [VoiceMeeter](https://vb-audio.com/Voicemeeter/)
4. **NachoBot Core** 处于运行状态
5. 相邻的 **NachoBot-Multimodal-Adapter** 已准备共享 ASR 配置和模型

## 🎛️ 输入/输出设备设置指南

为了让 Bot 能够听到频道内其他人的声音，并让频道内的人能听到 Bot 的声音，请按照以下步骤配置音频路由：

### 1. 适配器配置 (`config.toml`)
- **`[capture] target_process_name`**：填写目标语音软件的进程名（例如 `"QQ.exe"`、`"Discord.exe"`）。这是 **Bot 的耳朵**，适配器会自动从该进程中捕获其他人说话的声音。
- **`[output] device_name`**：填写虚拟声卡的**输入端**名称（例如 `"CABLE Input (VB-Audio Virtual Cable)"`）。这是 **Bot 的嘴巴**，Bot 的 TTS 回复会播放到这个虚拟声卡中。

### 2. 语音软件配置 (以 Discord/QQ/游戏 为例)
进入该目标语音软件的音频设置界面：
- **输出设备 (扬声器/播放)**：保持为你**日常使用的耳机或扬声器**。你只需正常听声音即可，适配器是直接在进程层面捕获音频的，无需修改输出设备。
- **输入设备 (麦克风/录音)**：修改为虚拟声卡的**输出端**（例如 `"CABLE Output (VB-Audio Virtual Cable)"`）。这样，Bot 播放到虚拟声卡里的声音，就会作为麦克风输入发送给频道里的其他人。

> 💡 **提示：如何实现你与 Bot 同时说话？**
> 如果你把语音软件的麦克风改成了虚拟声卡，别人就听不到你本人的声音了。若想**你和 Bot 一起在频道里说话**，需要使用 [VoiceMeeter](https://vb-audio.com/Voicemeeter/) 等混音软件，将你的**物理麦克风**和 **Bot的音频** 混合后，作为单一麦克风输入提供给语音软件。

## 🚀 快速开始

```bash
# 1. 安装依赖 (使用 uv)
cd NachoBot-UniversalVC-Adapter
uv sync

# 2. 编辑现有 config.toml
# 修改 target_process_name、device_name 与各流水线开关

# 3. 运行适配器（初次运行会检查并下载所需模型）
# 也可直接运行项目根目录的 launch_universal_vc.bat
uv run python main.py
```

## ⚙️ 配置文件 (`config.toml`) 亮点

新版配置支持对流水线各个节点进行独立控制：

```toml
[capture]
target_process_name = "QQ.exe"  # 捕获目标

[output]
device_name = "VoiceMeeter Input" # 虚拟声卡名称

[denoise]
enabled = true # 强烈建议开启，后台批处理几乎无性能影响

[vad]
threshold = 0.5
min_speech_duration = 0.25
min_silence_duration = 0.3 # 控制切断语句的间隔

[speaker]
enabled = true
similarity_threshold = 0.6 # 声纹区分灵敏度

```

流式 ASR 不再在 UniversalVC 中重复配置。请在相邻的
`NachoBot-Multimodal-Adapter/configs/perception.toml` 中统一设置：

```toml
[asr]
mode = "local_streaming"
provider = "cpu"
num_threads = 4
models_dir = "models"
auto_download = true
```

## 🧩 架构流水线工作原理

相比旧版的堵塞式线性架构，新版适配器采用异步管线分离了高速音频捕获和重负载 AI 推理：

```text
 目标进程 ─[1. WASAPI]─→ 48kHz 无损捕获
                          │
                          ↓
                   [2. 直接重采样 16kHz]
                          │
                          ↓
                   [3. Silero VAD] ──── (静音判定/段落切分)
                          │
                          ↓
    ┌────────────────[异步后台处理池]─────────────────┐
    │                                               │
    │ [4. 批量去噪] 48kHz RNNoise (过滤环境噪音)    │
    │                                               │
    │ [5. 声纹特征] WeSpeaker 提取特征并分配 ID     │
    │                                               │
    │ [6. 语音识别] xlarge INT8 Zipformer (CPU流式) │
    └───────────────────────────────────────────────┘
                          │
                          ↓
             NachoBot Core (附带 UserInfo)
                          │
                          ↓
            虚拟声卡 ←─ TTS ←─ 回复文本
```

## 📂 目录结构概述

```
NachoBot-UniversalVC-Adapter/
├── main.py               # 入口程序，负责检查环境与依赖
├── model_manager.py      # VAD 与声纹模型下载管理器
├── multimodal_bridge.py  # 接入 Multimodal 共享 ASR 包
├── audio_pipeline.py     # 核心组件: 异步音频调度流水线
├── denoise.py            # RNNoise 降噪封装
├── vad_processor.py      # Silero VAD 封装
├── speaker_tracker.py    # WeSpeaker 声纹与数据库聚类
├── adapter.py            # ncnk_message 发送与流程控制
├── audio_capture.py      # 进程音频捕获
├── audio_output.py       # 虚拟声卡音频输出
├── speaker_db.json       # 本地声纹数据库 (运行时自动生成)
└── download_models.py    # 手动触发模型下载脚本
```

## Docker 部署

本适配器提供 Windows 容器镜像：

```bat
docker network create nacho_bot
docker compose up -d
```

镜像使用 Windows 容器引擎，因为 `proc-tap`、WASAPI 和虚拟声卡均依赖
Windows。容器只能访问容器内部可见的进程和音频设备，不能直接捕获宿主机上的
Discord/QQ/游戏进程或 VB-Audio 设备；如需这些能力，建议在宿主机直接运行。
运行容器时请将 `config.toml` 中的核心地址改为 `core`（或可达的宿主机地址），
并保留 `models`、`speaker_db.json` 及多模态模型卷。
