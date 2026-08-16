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

双击：

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
