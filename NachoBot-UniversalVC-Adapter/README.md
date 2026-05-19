# NachoBot Universal Voice Adapter

基于 [ProcTap](https://github.com/m96-chan/ProcTap) 的通用语音适配器，可捕获任意指定进程的音频（ASR识别为文本），并将 NachoBot Core 的回复通过 TTS 输出至虚拟声卡（如 VB-Audio Virtual Cable），实现在任意平台上的语音交互。

## 特性

- 🎧 **进程级音频捕获** — 使用 ProcTap (WASAPI) 捕获指定进程的音频流
- 🎙 **语音识别 (ASR)** — 内置 VAD + ASR API 调用，将语音转换为文本
- 🤖 **NachoBot 接入** — 通过 ncnk_message Router 连接 NachoBot Core
- 🔊 **虚拟声卡输出** — TTS 生成的语音通过 sounddevice 播放到虚拟声卡
- ⚡ **低延迟优化** — 自动 bypass Planner/记忆检索/工具调用

## 前置要求

1. **Windows 10/11** (20H1+)
2. **Python 3.10+**
3. **虚拟声卡驱动** — 推荐 [VB-Audio Virtual Cable](https://vb-audio.com/Cable/)
4. **NachoBot Core** 运行中

## 快速开始

```bash
# 1. 安装依赖
cd NachoBot-UniversalVC-Adapter
uv sync

# 2. 复制并编辑配置文件
copy config.toml.example config.toml
# 编辑 config.toml，设置 target_process_name 和 device_name

# 3. 运行
uv run python main.py
```

## 配置说明

```toml
[capture]
target_process_name = "Discord.exe"  # 或者直接设 target_pid = 12345
vad_threshold = 500                  # VAD 灵敏度（越低越灵敏）
silence_threshold = 0.8              # 静音判定时间（秒）

[output]
device_name = "CABLE Input"          # 虚拟声卡设备名
```

## 工作原理

```
目标进程 ─→ ProcTap (WASAPI) ─→ VAD ─→ ASR ─→ NachoBot Core
                                                      ↓
虚拟声卡 ←─ sounddevice ←─ WAV ←─ TTS ←─ 回复文本
```

## 目录结构

```
NachoBot-UniversalVC-Adapter/
├── main.py            # 入口
├── adapter.py         # 核心适配逻辑
├── audio_capture.py   # ProcTap 音频捕获 + VAD
├── audio_output.py    # 虚拟声卡音频输出
├── tts_handler.py     # TTS (GPT-SoVITS)
├── config.py          # 配置定义
├── config.toml        # 配置文件
└── requirements.txt   # 依赖
```
