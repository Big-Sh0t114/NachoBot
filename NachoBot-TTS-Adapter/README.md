# NachoBot TTS 适配器

基于GPT-SoVITS的文本转语音 (TTS) 适配器，支持流式和非流式语音合成。  
本仓库在原上游 `maimbot_tts_adapter` 基础上，已针对 **NachoBot 核心改动** 做了深度的本地化和一键启动化改造。适配器依赖工作区根目录下的 `launchbot.bat` 进行环境的自动管理和拉起。

---

由于本系统将 TTS 的隔离环境、依赖库安装和启动交给了项目最外层的 `launchbot.bat` 统一接管，请按照本指南配置物理路径与参数。

## 1. 前置要求：独立的 GPT-SoVITS 端
本适配器本质上是一个中继网关，负责将 NachoBot 的文本发送给对应的 TTS 引擎生成语音流。
- 你必须在你的电脑上拥有独立安装好的 **GPT-SoVITS** 环境（推荐 v2pro 版本）。
- 请确保你拥有训练好/微调好的 GPT 权重文件（`.ckpt`）和 SoVITS 权重文件（`.pth`），以及一段用于音色的**参考音频**（`.wav`）。

## 2. 环境依赖与启动前准备
虽然最外层的 `launchbot.bat` 会尽可能帮你自动创建 `.venv` 虚拟环境并安装依赖，但为了确保环境一致性，你也可以在启动前手动确认组件依赖安装成功：
进入 `NachoBot-TTS-Adapter` 目录，安装该适配器专属依赖包（推荐使用项目统一的 `uv`）：
```bash
uv pip install -r requirements.txt
```

## 3. 对接 GPT-SoVITS 路径 (`launchbot.bat`)
回到 NachoBot 根目录，使用文本编辑器打开 `launchbot.bat`：
找到大约第 36 行左右的 `SOVITS_DIR` 变量，将其修改为你电脑上实际存放 GPT-SoVITS 整合包的**绝对硬盘路径**：
```bat
REM ===== 基本路径（如你改过目录，只需改这里）=====
set "SOVITS_DIR=C:\Users\BigSh0t\GPT-SoVITS\GPT-SoVITS-v2pro-20250604" 
```

## 4. 配置实际模型路径 (`configs/base.toml`)
进入本目录的 `configs/` 文件夹下。
你可以把你的声音模型直接放到这里。
打开 `configs/base.toml`，找到最下方的 `[plugins.GPT_Sovits]` 模块，将其中的 `gpt_weights` 和 `sovits_weights` 的文件路径修改为你实际存放权重的**绝对路径**：
```toml
[plugins.GPT_Sovits]
api_base = "http://127.0.0.1:9874"
gpt_weights    = "C:/Users/.../NachoBot-TTS-Adapter/configs/EXAMPLE.ckpt"
sovits_weights = "C:/Users/.../NachoBot-TTS-Adapter/configs/EXAMPLE.pth"
speaker = "NachoChan"
```

## 5. 绑定核心路由规则 (`configs/base.toml`)
为了能让 NachoBot 发出的文本传到这里，你必须配置连接通道。同样在 `configs/base.toml` 里，确认 `[routes]` 块下的端口与主控台一致（NachoBot 默认跑在 `8000` 端口）：
```toml
[routes]
qq = "http://127.0.0.1:8000/ws"        # 对应你在主干代码里的监听地址
discord = "http://127.0.0.1:8000/ws"
```

## 6. 调整声音预设参数 (`configs/gpt-sovits.toml`)
同样在 `configs/` 文件夹下，打开 `gpt-sovits.toml`，找到最下方的预设管理 `[tts.models.presets.default]`（以及你自定义的角色如 `custom1`）：
你需要重点填写控制音色感情的特征绑定：
```toml
[tts.models.presets.default]
name = "DEFAULT"
gpt_model = "EXAMPLE.ckpt"
sovits_model = "EXAMPLE.pth"
ref_audio_path = "EXAMPLE.wav"   # 参考音频的文件名或相对路径
prompt_text = "填入你参考音频里正在说的那句话"  # 参考音频对应的精确文本
prompt_language = "ja"                   # 参考文本的语种 (ja/zh/en/auto)
speed_factor = 1.0                       # 生成语速
```

---


**请勿单独在此适配器目录内运行 python 启动接口。**

配置完成后，只需回到根目录双击运行的 **`launchbot.bat`**。
脚本将按照以下时序全自动并线拉起所有组件（你会看到多个黑色命令台窗口同时工作）：

1. **SoVITS API (监听 9880 端口)**：调用你的 GPT-SoVITS 目录环境，启动底层语音引擎 API。
2. **TTS Adapter (监听 8070 端口)**：待 SoVITS API 存活后，拉起本适配器并建立与 NachoBot 核心（默认 8000 端口）的双向 WebSocket 隧道。
3. **Control API (监听 9872 端口)**：承载高级配置控制的额外端口。
4. **NachoBot 主服务 + NapCat 适配器**：启动主脑逻辑处理区，随后连入 QQ/Bilibili。

如遇 TTS 模块报错或静音，请检查是否是 `.ckpt` 或 `.pth` 文件路径缺失，或是端口 `9880/8070` 被系统中其他程序意外占用。

---

## Docker 部署

本适配器同样支持独立 Docker 部署（目前仅容器化**适配器**部分，**GPT-SoVITS 本身仍需在宿主机或单独服务中运行**）。

### 启动方式

```bash
# 在本目录下执行
docker compose up -d
```

### Docker 注意事项
- 本容器依赖外部的 `nacho_bot` Docker 网络。若未创建请先执行 `docker network create nacho_bot`。
- **连接至 GPT-SoVITS API**：在 `configs/base.toml` 中，`[plugins.GPT_Sovits].api_base` 默认是指向 `127.0.0.1:9874`。如果你在 Docker 中运行，且 GPT-SoVITS 在宿主机上，请将此处的 IP 改为宿主机的局域网 IP，或者修改为 `host.docker.internal`。
- **模型文件路径**：`configs/base.toml` 和 `configs/gpt-sovits.toml` 中指定的模型路径（`.ckpt`, `.pth`, `.wav`）**必须是基于容器内的路径**（即挂载后的路径，通常位于 `/nachobot_tts_adapter/configs/` 下）。
- 容器会自动挂载 `./configs` 目录和 `./logs` 目录。修改配置后重启容器生效。
