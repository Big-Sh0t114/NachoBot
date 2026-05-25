# NachoBot 多功能语音与感知适配器 (TTS & Perception Adapter)

本仓库基于 `maimbot_tts_adapter` 进行了深度定制与本地化改造，已成为 **NachoBot 生态的核心多媒体中继网关**。

它不仅支持基于 **GPT-SoVITS** 与 **Vox (VoxCPM)** 的高性能文本转语音 (TTS) 功能，还集成了服务端**零样本情感分类系统**、跨模块**情感查询 API**，以及独立的**感知系统 (Perception API)** ── 提供符合 OpenAI 规范的 **ASR 语音识别 (FunASR)** 与 **VLM 视觉理解 (Florence-2)** 接口。

---

## 🌟 核心特性

### 1. ⚔️ 互斥双 TTS 后端支持
在 `configs/base.toml` 的 `[enabled_tts]` 中可切换启用的后端（二选一）：
*   **GPT-SoVITS 引擎**：支持超高还原度的 Zero-shot 声音克隆、在线权重动态切换、多说话人音色融合与流式/非流式语音合成。
*   **Vox (VoxCPM/LocDiT) 引擎**：基于最前沿的流匹配 (Flow Matching) 语音克隆技术，支持极致克隆模式与声音描述 (Voice Design)，生成更为自然生动的合成语音。

### 2. 🧠 服务端情感分类系统 (Emotion System)
*   **多语言 Zero-shot 分类**：使用 `mDeBERTa-v3-base-xnli` 情感分类模型，动态推理输入文本的情感倾向（如 Happy, Sad, Angry, Sleepy 等）。
*   **预设自动路由**：分类器根据置信度阈值将文本归类为指定情感标签，并自动加载对应的声音预设（`.wav` 参考音频与对应参数），使 Bot 的声调随着说话内容和语境产生细腻的情感起伏。
*   **置信度回退**：当分类置信度低于设定阈值（如 `0.6`）时，自动安全回退至系统默认音色，避免情感漂移。
*   **GPU 加速与 FP16 推理**：支持配置运行设备（CUDA/CPU）并自动启用半精度推理，大幅减少显存与内存开销。

### 3. 🌐 情感查询共享接口 (`/api/emotion_preset`)
*   适配器内置轻量级 HTTP 接口。
*   供外部独立语音模块（如 `DiscordVC-Adapter`、`Bilibili-Adapter`、`UniversalVC` 等）远程调用。外部客户端无需安装 PyTorch、Transformers 等重型 AI 库，即可直接获取最佳声音预设名称，实现全局情感统一。

### 4. 👁️ 独立感知服务 (Perception API - ASR & VLM)
由 `tts_src/plugins/Perception/api_server.py` 承载的独立多模态感知端，提供完全兼容 OpenAI 规范的 API 接口：
*   **VLM (视觉理解)**：由大模型 `Florence-2-large` 驱动，解析 Base64 编码的图片并自动生成详细描述。
    *   **接口**：`POST /v1/chat/completions` (OpenAI 兼容)
*   **ASR (语音识别)**：由 `FunASR (SenseVoice-small)` 驱动，提供快速、精准的语音转文字服务。
    *   **接口**：`POST /v1/audio/transcriptions` (OpenAI 兼容)
*   **按需开启**：支持通过环境变量 `DISABLE_VLM_ASR=1` 跳过感知组件加载，极大地节省系统开销。

---

## 📂 项目结构

```text
NachoBot-TTS-Adapter/
├── configs/                  # 运行期配置文件 (base.toml, vox.toml, gpt-sovits.toml, perception.toml)
├── template_configs/         # 配置模板文件 (各配置文件的默认干净模板)
├── tts_src/
│   ├── plugins/
│   │   ├── GPT_Sovits/       # GPT-SoVITS 客户端适配
│   │   ├── Vox/              # VoxCPM 适配、LoRA 微调集成与推理服务
│   │   └── Perception/       # VLM (Florence-2) & ASR (FunASR) 独立微服务
│   └── utils/
│       ├── emotion_classifier.py  # 零样本情感分类器
│       ├── text_cleaner.py        # 文本正则化与标点清理
│       └── text_splitter.py       # 智能切长句分段器
├── main.py                   # 适配器主入口 (包含双向 WS 转发路由与 HTTP 接口)
├── tts_model_debugger.py     # TTS 音质和配置本地测试脚本
└── requirements.txt          # Python 依赖清单
```

---

## 🔧 配置指南

进入 `configs/` 目录，配置以下关键文件（若没有，可从 `template_configs/` 复制并重命名）：

### 1. 基础路由控制 (`configs/base.toml`)
```toml
[server]
host = "127.0.0.1"
port = 8070                 # 本适配器监听的端口

[routes]
qq = "http://127.0.0.1:8000/ws"        # NachoBot 核心的 WebSocket 监听地址
discord = "http://127.0.0.1:8000/ws"

[enabled_tts]
enabled = ["Vox"]           # 启用的TTS模块: "GPT_Sovits" 或 "Vox" (二选一)

[tts_base_config]
stream_mode = false         # 是否开启流式合成 (流式可大幅降低首字延迟)
```

### 2. GPT-SoVITS 参数配置 (`configs/gpt-sovits.toml`)
*   `[tts]` 下配置 GPT-SoVITS 底层 API 服务的物理路径与端口（默认 `9880`）。
*   在 `[tts.models.presets]` 中注册声音预设，指定相应的 `.ckpt`、`.pth` 权重及 `.wav` 参考音频路径。

### 3. Vox (VoxCPM) 参数与情感分类配置 (`configs/vox.toml`)
```toml
[tts]
host = "127.0.0.1"
port = 9880
model_dir = "C:/Users/BigSh0t/VoxCPM-2.0.2/models/openbmb__VoxCPM2"
lora_weights_path = "C:/Users/BigSh0t/VoxCPM-2.0.2/lora/ncnk3"  # 选填，LoRA微调路径

# 角色音色预设（支持参考音频极致克隆）
[tts.models.presets.happy]
name = "happy"
ref_audio_path = "happy.WAV"
prompt_text = "填入参考音频对应的精确文本内容..."

# 情感自动感知系统
[emotion]
enabled = true
classifier_model = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"
classifier_device = "cuda:0"   # 使用 "cpu" 或 "cuda:0"
use_fp16 = true               # CUDA模式下开启半精度
confidence_threshold = 0.6    # 分类置信度阈值
default_emotion = "default"

# 情感标签 -> 音色预设映射表
[emotion.tag_preset_map]
default = "default"
"disgust/unhappy" = "disgust"
"happy/excited" = "happy"
"sad/sexual" = "sad"
```

### 4. 独立感知配置 (`configs/perception.toml`)
```toml
[perception]
host = "127.0.0.1"
port = 9874                 # Perception API 服务监听的端口

[perception.device]
vlm = "cuda:0"              # Florence-2 运行设备
asr = "cuda:0"              # FunASR 运行设备
```

---

## 🚀 启动与调试

### A. 全自动一键拉起 (推荐)
回到 NachoBot 项目的根目录下，直接双击运行 **`launchbot.bat`**。
启动脚本会按顺序并行启动并监控：
1. **SoVITS/Vox API**：加载底层语音克隆引擎。
2. **Perception API**：拉起 Florence-2 + FunASR 感知服务端（监听 `9874` 端口）。
3. **TTS Adapter**：拉起本适配器（监听 `8070` 端口）并桥接 NachoBot 核心（`8000` 端口）。
4. **NachoBot Core**：加载主控大脑逻辑。

> [!NOTE]
> 运行前，请务必在 `launchbot.bat` 大约第 36 行将 `SOVITS_DIR` 指向你本机的 GPT-SoVITS 物理存放路径。

### B. 手动依赖安装与启动
如果你需要独立调试，可按照以下步骤进行：
1. **创建环境并安装依赖**（推荐使用更高效的 `uv` 工具）：
   ```bash
   uv pip install -r requirements.txt
   ```
2. **运行调试器测试 TTS 输出**：
   ```bash
   python tts_model_debugger.py
   ```
   调试器会模拟客户端向已启用的后端发送一段测试文本，合成成功后即可在控制台看到生成的音频数据流大小。

---

## 🐳 Docker 部署

本适配器同样支持完全容器化部署（TTS Adapter 服务 + 独立 Perception 感知服务）。

> [!WARNING]
> GPT-SoVITS / Vox 的底层 API 推理服务通常需要直接调取宿主机复杂的 GPU 驱动及专有环境，建议将其部署在宿主机，容器内通过配置局域网或宿主机网关桥接连接。

### 1. 快速启动
在本目录下直接执行：
```bash
docker compose up -d
```

### 2. Docker 环境注意事项
*   **共享网络**：本服务依赖外部自定义桥接网络 `nacho_bot`。如果尚未创建该网络，请先执行：
    ```bash
    docker network create nacho_bot
    ```
*   **通信地址**：在 Docker 容器中连接宿主机的 GPT-SoVITS API 时，请将 `configs/base.toml` 或 `configs/vox.toml` 中的 `api_base`/`host` IP 改为 `host.docker.internal`（Windows 宿主机桥接）或宿主机实际局域网 IP。
*   **路径映射**：容器会自动将宿主机的 `./configs` 与 `./logs` 目录挂载到容器内 `/nachobot_tts_adapter/` 对应路径下。修改宿主机配置文件后，执行 `docker compose restart` 即可热生效。
