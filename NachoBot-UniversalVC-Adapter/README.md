# NachoBot Universal Voice Adapter (Real-Time Edition)

基于 [ProcTap](https://github.com/m96-chan/ProcTap) 的通用语音连麦适配器。能够从任意指定进程（如 Discord、QQ、游戏等）无损捕获音频，并通过 **高性能实时语音流处理管线**（去噪、声纹识别、流式语音识别）将语音转化为带有说话人身份标识的文本，发送至 NachoBot Core。Bot 的回复也将通过虚拟声卡返回至游戏或语音软件中。

本适配器经过全面升级，专为**多人连麦/游戏场景**设计。

## 🌟 核心特性

- 🎧 **进程级无损捕获** — 使用 ProcTap (WASAPI) 提取特定进程的原始 PCM 数据。
- 🔇 **智能批量去噪 (RNNoise)** — 内置基于 `pyrnnoise` 的深度学习降噪。VAD 触发后异步进行批量降噪，在保证低至毫秒级捕获延迟的同时，完美过滤游戏底噪、键盘声与环境噪音。
- 🎙 **高精度语音活动检测 (Silero VAD)** — 通过 `sherpa-onnx` 运行，比传统 RMS 阈值拥有更高的精准度，能完美切分连续对话。
- 👥 **实时声纹追踪 (WeSpeaker)** — 在多人频道中，能够通过声纹特征 (WeSpeaker ResNet34) 自动聚类并追踪说话人，给 NachoBot Core 提供说话人 ID 区分上下文！
- ⚡ **本地流式语音识别 (Zipformer)** — 采用全本地化、支持流式的 Zipformer ASR 模型，无需网络调用，实现极致低延迟语音转文本。
- 📦 **自动化部署** — 启动时自动从云端拉取所需的所有 ONNX 模型（含断点续传），内置 DLL 劫持防御，免管理员权限。

## 💻 前置要求

1. **Windows 10/11** (20H1+)
2. **Python 3.10+** (推荐使用 `uv` 管理依赖)
3. **虚拟声卡驱动** — 推荐 [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) 或 [VoiceMeeter](https://vb-audio.com/Voicemeeter/)
4. **NachoBot Core** 处于运行状态

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

# 2. 复制并编辑配置文件
copy config.toml.example config.toml
# 根据需要修改 target_process_name 和 device_name，以及各项特性的开关

# 3. 运行适配器 (初次运行会自动下载所需 AI 模型)
# 推荐使用项目根目录的 launch_universal_vc.bat 脚本来启动
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

[local_asr]
mode = "local_streaming" # 可选: local_streaming 或 remote_api (后备方案)
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
    │ [6. 语音识别] 流式 Zipformer (本地高精度转换) │
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
├── model_manager.py      # AI 模型自动下载与验证管理器
├── audio_pipeline.py     # 核心组件: 异步音频调度流水线
├── denoise.py            # RNNoise 降噪封装
├── vad_processor.py      # Silero VAD 封装
├── speaker_tracker.py    # WeSpeaker 声纹与数据库聚类
├── streaming_asr.py      # Sherpa-onnx 本地流式识别
├── adapter.py            # ncnk_message 发送与流程控制
├── audio_capture.py      # 进程音频捕获
├── audio_output.py       # 虚拟声卡音频输出
├── speaker_db.json       # 本地声纹数据库 (运行时自动生成)
└── download_models.py    # 手动触发模型下载脚本
```
