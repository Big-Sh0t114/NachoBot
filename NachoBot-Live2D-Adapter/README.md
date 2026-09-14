# NachoBot Live2D Adapter

独立的 Live2D 渲染进程。它通过版本化 WebSocket JSON 协议接收平台无关的虚拟形象命令，并将点击、戳一戳等交互事件回传给调用方。

本适配器不依赖 Bilibili 消息对象、NachoBot 聊天模型、数据库或 LLM 客户端，因此也可被其他平台复用。

## 架构

```text
NachoBot / 平台适配器
        │
        │ avatar.command (WebSocket JSON)
        ▼
NachoBot-Live2D-Adapter
        │
         ├─ protocol.py   版本化协议
         ├─ action_adapter.py 情绪/问题到 canonical action 的平台无关决策
         ├─ server.py     WebSocket 服务
        ├─ runtime.py    协议到渲染命令的转换
        └─ renderer.py   PyGame/OpenGL/Live2D 渲染
        │
        └─ avatar.interaction → ready / click / poke / error
```

Bilibili 侧通过 `bili_src/live2d/remote_controller.py` 连接本服务。旧的本地 `live2d_render` 实现已从 Bilibili Adapter 主项目移出，并保存在工作区级归档目录中。

## 环境要求

- Windows
- Python 3.11 或更高版本
- Live2D Python 绑定及其原生运行库（由 `live2d-py` wheel 提供）
- 模型所需的 `.model3.json`、`.moc3`、纹理、动作和表情资源

安装 [uv](https://docs.astral.sh/uv/) 并同步项目声明的 Python 依赖：

```bat
cd NachoBot-Live2D-Adapter
uv sync
```

`live2d-py` 已声明在 `pyproject.toml` 中，`uv sync` 会安装与当前 Windows/Python 版本匹配的 wheel。

## 配置

编辑 `config.toml`：

```toml
[server]
host = "127.0.0.1"
port = 8766
token = ""

[renderer]
model_path = "resources/NachoBot/Nachobot.model3.json"
transparent = true
antialiasing = true
width = 1400
height = 1200
scale = 1.0
track_mouse = false
poke_cooldown_seconds = 10.0
```

`model_path` 相对于 `config.toml` 所在目录解析。

### 自动模型适配

默认启用非破坏性的模型适配层：模型启动时读取 `.model3.json`、可选的
`.cdi3.json`，并在加载完成后结合 `live2d-py` 实际枚举出的参数、表情和
Motion Group 建立运行时映射。适配过程不会改写用户的 `.model3.json`、
`.moc3` 或其他模型资源。

```toml
[adaptation]
enabled = true
```

自动适配包括：

- 从 `FileReferences.Moc` 读取真实 `.moc3` 路径，不要求它与 `.model3.json` 同名。
- 优先使用模型声明的 `LipSync` 参数；声明明显误指向眼睛等冲突参数时，自动寻找高置信嘴型参数。
- 按实际名称和常见中、英、日文语义匹配表情与 canonical action。
- `param_tween` 既接受模型原始参数 ID，也接受 `MOUTH_OPEN`、`MOUTH_FORM`、
  `ANGLE_X/Y/Z`、`BODY_ANGLE_X/Y/Z`、`EYE_OPEN`、`EYE_L_OPEN`、`EYE_R_OPEN`、
  `EYE_BALL_X/Y`、`BROW_L_Y`、`BROW_R_Y` 和 `BREATH` 等稳定字段。

只有唯一或有明确模型元数据支持的映射才会自动采用。无法确定时会记录告警，
可在配置中覆盖；数组表示一次控制多个联动参数：

```toml
[adaptation.parameters]
MOUTH_OPEN = ["ParamMouthOpenY"]

[adaptation.expressions]
normal = "normal"
shy = "shy"
disgust = "disgust"
angry = "angry"
```

### 动作映射

协议只传递稳定的 canonical action ID，具体 Motion Group 由本适配器配置：

```toml
[actions]
NOD = "Nod"
SHAKE_HEAD = "Shake"
TURN_LEFT = "TurnLeft"
TURN_RIGHT = "TurnRight"
WINK = "Wink"
HAPPY = "Sway"
TILT_HEAD = "TiltHead"
LOOK_AWAY = "LookAway"
```

配置的 Motion Group 存在时始终优先使用；不存在时，自动适配层会尝试匹配模型中
语义明确的动作名称。仍无法识别时只需修改该映射，不应在 NachoBot 或平台适配器中
写死模型 Motion Group。

## 启动

### Hiyori 桌面宠物

模型位于本适配器的 `resources\hiyori_test` 时，双击统一入口 `launch_live2d.bat`。启动器会同步依赖、
在后台启动透明窗口并确认 WebSocket 端口实际监听。配置使用相对路径，因此移动整个仓库后
无需改盘符；使用模型时仍须遵守模型目录中的 Live2D 示例模型许可。

桌宠操作：

- 人物脚下会常驻米白、藏蓝、樱粉配色的无边框聊天窗；双方消息按 QQ 式左右气泡排列，输入后按 Enter 发送。
- 输入条内可直接切换“声音：开 / 闭嘴中”和 TTS 语言（自动、中文、日语、英语）；“说明”会展开命令帮助。
- 输入条会跟随人物窗口移动；点右上角 `—` 可暂时收起，双击人物会重新显示并聚焦。
- 左键拖动桌宠窗口；双击会聚焦输入条，右键触发动作。
- `Shift + 左键` 拖动可调整人物在窗口内的位置，滚轮缩放人物。
- 系统托盘菜单可以显示/隐藏、切换鼠标穿透、切换置顶、复位位置或退出。
- 窗口位置、缩放和开关状态会写入本地 `desktop_pet_state.json`，不会改动模型资源。
- 最近 100 条双方消息保存在本地 `desktop_pet_chat_history.json`，重启桌宠后仍可向上翻看；Core 使用固定本机会话身份维持连续问答上下文。
- Local Host 和 Bilibili 只提供问题/回复元数据，`action_adapter.py` 统一选择 canonical 动作：疑问歪头、否定摇头、夸奖/开心身体晃动、害羞移开视线；模型实际动作组仍由本适配器的 `[actions]` 映射解析。
- NachoBot 后端可继续通过 `ws://127.0.0.1:8766` 发送动作、情绪、视线、说话和音频命令。

运行方式由 `config.toml` 的 `[runtime].mode` 决定：`desktop_pet` 启动桌宠和聊天依赖，
`live` 只启动直播窗口与 WebSocket 适配器。如果模型移动了，只需修改 `model_path`。

脚边输入框支持：

- 普通文字：经 `NachoBot-Local-Host-Adapter` 发送给 NachoBot Core，回答后自动播放本地语音。
- `/说 内容`：不经过 AI，直接生成并朗读指定内容。
- `/闭嘴`：继续显示 Core 的文字回答，但立即停止且不再生成 TTS 或口型；`/开口` 恢复。
- `/语言 自动|中文|日语|英语`：设置后续 TTS 的语言提示；默认“自动”会让 VoxCPM2 自行识别。
- `/动作 开心|点头|摇头|挥手|害羞` 和 `/表情 开心|害羞|生气|惊讶|悲伤|正常`。
- `/置顶`、`/穿透`、`/隐藏`、`/复位`。
- `/打开 记事本|计算器|文件管理器`；只执行这三个白名单程序，不接受任意 Shell 命令。
- `/帮助`：在输入框内显示完整命令说明。

输入框中按 `Ctrl + Enter` 会把当前文字直接朗读，不经过 AI；闭嘴模式下会提示先恢复声音。

桌宠模式下，`launch_live2d.bat` 会一并检查并启动 NachoBot Core、本机问答桥和 VoxCPM2 TTS；地址由
`[desktop_pet.chat].backend_url` 配置。服务不可用时输入框会显示明确错误，不会静默执行。

### 通用适配器

将 `config.toml` 中的 `mode` 改为 `live` 后，仍双击同一个入口：

```text
launch_live2d.bat
```

或者手动执行：

```bat
uv run python -m live2d_adapter --config config.toml
```

建议启动顺序：

1. 启动 `NachoBot-Live2D-Adapter`。
2. 确认日志显示 WebSocket 服务监听 `127.0.0.1:8766`。
3. 启动 `NachoBot-Bilibili-Adapter`。
4. Bilibili 侧日志应显示已连接独立 Live2D Adapter，并收到 `ready` 事件。

Bilibili Adapter 的 `[live]` 配置：

```toml
enable_live2D = true
live2d_url = "ws://127.0.0.1:8766"
live2d_token = ""
live2d_reconnect_seconds = 3.0
```

当服务端配置了 token 时，两侧值必须一致。客户端会把 token 作为 WebSocket 查询参数传递。

## 协议

当前协议版本：`1.0`。

### 命令信封

```json
{
  "type": "avatar.command",
  "version": "1.0",
  "request_id": "optional-request-id",
  "event": "state",
  "payload": {
    "state": "start_replying"
  }
}
```

支持的命令事件：

- `state`
- `speaking`
- `emotion`
- `action`
- `motion`
- `random_motion`
- `gaze`
- `param_tween`
- `ping`
- `shutdown`

### 交互信封

```json
{
  "type": "avatar.interaction",
  "version": "1.0",
  "event": "ready",
  "payload": {
    "running": true
  }
}
```

支持的交互事件：

- `ready`
- `click`
- `poke`
- `pong`
- `error`

协议只保证主版本兼容。客户端和服务端的 major version 不一致时，服务端会返回协议错误。

## 交互行为

- 鼠标左键拖动模型。
- 鼠标右键拖动透明窗口。
- 鼠标滚轮缩放模型。
- 鼠标侧键 6 或 7 触发 `click`；通过冷却检查后额外触发 `poke`。
- `track_mouse = true` 时持续跟踪鼠标视线。
- `speaking` 命令控制嘴部参数动画。

## 组件边界

- 渲染实现和模型资源均已移动到本项目。
- Bilibili Adapter 仅使用远程 WebSocket 控制器。
- Bilibili Adapter 不再为了 Live2D 构造 NachoBot `MessageRecv` 或模拟消息流。
- 主运行路径不再导入旧本地控制器、动作管理器、情绪管理器或渲染器桥接模块。

## 故障排查

### `import live2d.v3` 失败

在项目目录执行 `uv sync`，然后用 `uv run python -c "import live2d.v3"` 验证绑定可从项目虚拟环境导入。

### 模型窗口启动后立即退出

检查：

- `model_path` 是否指向真实的 `.model3.json`。
- 同目录是否存在对应 `.moc3`。
- 模型 JSON 引用的纹理、动作和表情文件是否完整。

### Bilibili 侧持续重连

检查：

- 独立 Adapter 是否已启动。
- 两侧端口是否一致。
- token 是否一致。
- 防火墙是否允许对应监听地址和端口。

### 动作命令返回 `unmapped canonical action`

在 `[actions]` 中为该 canonical action ID 配置模型实际存在的 Motion Group。

### 日志提示无法自动识别参数或表情

先检查模型是否带有正确的 `Groups`/`DisplayInfo` 元数据；若模型使用自定义或无语义
ID，在 `[adaptation.parameters]` 或 `[adaptation.expressions]` 中添加显式映射。
适配器不会为了猜测语义而修改原始模型文件。

## Docker 部署

本适配器提供 Windows 容器镜像：

```bat
docker network create nacho_bot
docker compose up -d
```

`live2d-py` 仅提供 Windows 原生 wheel，因此必须切换 Docker Desktop 的
Windows containers 引擎。容器不会自动获得宿主机的桌面窗口、OBS 捕获链路或
音频设备；需要实际显示模型并联动 OBS 时，仍建议在宿主机直接运行本适配器。
容器配置需将 `[server].host` 改为 `0.0.0.0`，Bilibili 侧的
`live2d_url` 使用容器可达的地址，而不是 `127.0.0.1`。
