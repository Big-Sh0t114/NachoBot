# NachoBot Desktop Pet Adapter（可选 Live2D 与 Sprite 渲染）

独立桌宠运行时提供透明桌宠、桌面感知、交互策略、聊天、语音、托盘、启动器和状态管理；`[renderer].backend` 可选择可选的 Live2D provider 或逐帧 Sprite 渲染。两个后端都通过版本化 WebSocket JSON 协议接收平台无关的虚拟形象命令，并将点击、戳一戳等交互事件回传给调用方。

本适配器不依赖 Bilibili 消息对象、NachoBot 聊天模型、数据库或 LLM 客户端，因此也可被其他平台复用。

## Sprite 桌宠

仓库另有一套独立的逐帧 Sprite 桌宠，默认使用包内
`desktop_pet_adapter/assets/nachobot_chibi/manifest.json` 和透明 PNG 图集。
从仓库根目录运行 `launch_sprite_pet.bat` 可启动 Sprite 模式；它沿用当前
`config.toml` 的 Core、聊天和托盘设置，并通过启动请求选择 `sprite` 后端。
常规 `launch_desktop_pet.bat` 使用配置中的后端，并仅在选择 Live2D 时安装
Live2D 组合项目。Sprite 模式不需要 provider 或 OpenGL 依赖。两种后端共用一个
会话管理器，切换后端会重启当前桌宠会话。

Sprite 专用参数可写在 `[sprite]` 中：`atlas_path`（可选）、`size_px`
（默认 176）、`movement_speed_px_per_second`（默认 44）、
`idle_pause_min_seconds`（默认 1.5）和 `idle_pause_max_seconds`（默认 4.5）。
清单支持每个状态单独提供 `frame_rects`，或使用均匀网格的 `row`/`frames`。
Sprite 状态保存在 `state/desktop_pet_sprite_state.json`。Sprite 可以自主漫游；
Live2D 继续支持手动拖动，但桌面互动控制器不会移动它。

Sprite 桌宠会在肩侧小机器人的屏幕显示累计的物理键盘按下次数与鼠标
按键次数总和。计数只保存数字汇总到 `state/input_counts.json`，不会保存具体按键、
文字或鼠标位置，也不计入鼠标移动、滚轮或模拟输入；此显示只用于 Sprite，Live2D
不变。自定义图集若未
提供每帧 `face_rects` 元数据，会保留原机器人的表情。清单中的 `face_rects` 是相对
各帧 tile 的 `[x, y, width, height]`，可选且每个状态的帧数必须匹配；非常小的自定义
脸框在无法容纳完整数字缩写时会显示 `+`。
首次启动计数时，已经处于按住状态的按键会在首次观察到按下时计一次；键盘重复按下
不重复计数，释放后再次按下才会增加。
切换计数来源会像重启监听一样重置当前按住状态；切换时可能有短暂漏计，不会把两路监听的
重叠结果相加。
计数变化时机器人屏幕会短暂亮起，并在旁边合并显示 `+N`；鼠标悬停机器人脸部会在
桌宠窗口内显示未缩写的总数、键盘按下次数和鼠标点击次数。
游戏中按键未被计数时，右键 Sprite 桌宠并选择“游戏计数兼容”，再按提示确认 UAC。
只有按需启用的游戏计数辅助进程会以管理员权限运行；关闭此项或退出桌宠会停止它。

## 架构

```text
NachoBot / 平台适配器
        │
        │ avatar.command (WebSocket JSON)
        ▼
desktop_pet_adapter（规范运行时）
        │
        ├─ protocol.py / server.py / runtime.py  协议与通用命令运行时
        ├─ desktop_scene.py / desktop_interactions.py / desktop_uia.py  桌面感知与安全交互
        ├─ desktop_controller.py / chat.py / tray.py  桌宠行为与界面生命周期
        └─ backend registry（renderer.backend，按需延迟加载）
                     ├─ desktop_pet_adapter.live2d_backend.create_backend → provider（OpenGL 与模型资源）
                     └─ desktop_pet_adapter.sprite_backend → SpriteRenderer（透明帧图集与 Win32 窗口）
        │
        └─ avatar.interaction → ready / click / poke / error
```

Bilibili 侧通过 `bili_src/live2d/remote_controller.py` 连接本服务。旧的本地 `live2d_render` 实现已从 Bilibili Adapter 主项目移出，并保存在工作区级归档目录中。

## 桌宠运行时迁移

`NachoBot-Desktop-Pet-Adapter` 是独立宿主项目，只打包 `desktop_pet_adapter` 通用桌宠运行时。Sprite 使用包内透明帧图集，不加载 Live2D 模型，也不需要 `NachoBot-Live2D-Adapter` 目录。Live2D 渲染实现和模型资源由独立的 `NachoBot-Live2D-Adapter` 提供；`renderers/live2d` 是可选组合项目，会把 provider 安装到自己的 `.venv`。两个后端共用桌面控制器、聊天、托盘及协议运行时。模型资源保留在 provider 项目内，不再复制进桌宠项目。

`desktop-pet-adapter` 是安装后提供的 console script；桌宠用户入口只有仓库根目录的 `launch_desktop_pet.bat`，`launch_sprite_pet.bat` 显式选择 Sprite。`[runtime].mode` 独立控制 `desktop_pet` 或 `live`，`[renderer].backend` 选择实际绘制后端，默认值为 `live2d`。只有选择 Live2D 后端时才需要 `.model3.json`、`.moc3` 等资源；Sprite 可以省略 `model_path`。

## 环境要求

- Windows
- Python 3.11 或更高版本
- pygame 与 Pillow（两个渲染后端使用）
- 选择 `backend = "live2d"` 时需要同一仓库中的 `NachoBot-Live2D-Adapter` 项目、Live2D Python 绑定及其原生运行库，以及模型所需的 `.model3.json`、`.moc3`、纹理、动作和表情资源
- 选择 `backend = "sprite"` 时使用包内透明 PNG 图集，不需要 Live2D 模型资源

安装 [uv](https://docs.astral.sh/uv/) 并同步基础依赖：

```bat
cd NachoBot-Desktop-Pet-Adapter
uv sync --locked
```

基础同步安装 Sprite 和通用桌宠运行时所需依赖，不会解析或安装 Live2D provider。把配置中的 `[renderer].backend` 设为 `"sprite"` 后，即可在没有 sibling Live2D 项目的环境中使用同一个 `launch_desktop_pet.bat`。选择 `"live2d"` 时，BAT 会根据解析后的配置同步隔离的 `renderers/live2d/.venv`；主桌宠环境保持独立。手动准备 Live2D 环境时，从本项目目录运行 `uv sync --project renderers/live2d --locked`。高级 CLI 可直接在隔离环境运行 `uv run --project renderers/live2d --locked python -m desktop_pet_adapter --config config.toml --mode live`。该组合项目会接入本宿主和 sibling provider。

## 配置

编辑 `config.toml`：

```toml
[server]
host = "127.0.0.1"
port = 8766
token = ""

[renderer]
backend = "live2d"
model_path = "../NachoBot-Live2D-Adapter/resources/NachoBot/NachoBot.model3.json"
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

### 桌宠主动对话

在 `config.toml` 的 `[desktop_pet.chat]` 下设置 `proactive = true` 开启，
也可悬停桌宠后直接点击侧栏的气泡图标开启/关闭：青绿表示开启，灰色表示关闭。
侧栏切换即时生效，仅影响当前会话，重启后读取配置；开启后等待配置的初始时长，关闭时取消尚未展示的主动回复。
切换开关本身不会清除未回应次数及退避，也不会重置用户发言时间；重新开启仍等待配置的初始时长。无核心模式不显示该按钮。
`proactive_idle_seconds = 300` 表示用户沉默满 5 分钟后开始判定：首次在满 5 分钟时判定，初始每 30 秒独立以 25% 概率触发，未命中则等待下一轮。
初始等待时长可设置为 30～86400 秒，功能默认关闭；基础判定间隔为 30 秒、概率为 25%，不增加配置项。
只有用户说话沉默满该时长，且 Windows 全局键盘和鼠标（包括鼠标移动）也连续无输入满该时长，才启用未回应退避。全局输入状态复用 `GetLastInputInfo`；非 Windows、读取失败或返回值无效时视为未知，清除退避计数与长间隔。键鼠活动只释放长退避，不重置用户说话计时、不取消正在生成的主动回复，也不阻止满足用户沉默条件后的正常主动开场。
用户消息或检测到的说话会重新计算沉默时间并清除退避；bot 发言只更新自己的 30 秒基础冷却，不重置用户计时。
满足上述双重空闲条件后，连续 3 次已展示的主动发言没有得到回应才开始退避：第 3 次后间隔变为 90 秒、概率约 16.67%；第 4 次后变为 270 秒、约 11.11%；第 5 次后变为 810 秒、约 7.41%。
此后每多一次未获回应的主动发言，判定间隔变为此前的 3 倍，概率变为此前的 2/3，不设置固定停止次数。
键鼠活动或未知状态会恢复 30 秒、25% 并清零次数；用户输入或说话也会清零。概率未命中、生成失败、取消或普通回复均不增加未回应次数，失败后按当前间隔和概率重试。
正在打字、有待发送语音、识别中或播放回复时跳过当轮，不补发。持续录音开启但没有说话时仍可主动交谈。
用户开始输入时会取消尚未展示的主动回复，优先处理用户消息；隐藏或退出桌宠也会取消。
启用视觉且有未过期的屏幕摘要时会结合画面交流，否则根据已有对话自然开场，不额外开启采集。
主动发言使用 Core 的桌宠私聊与结构化回复，只有 bot 正文进入桌宠聊天历史；朗读沿用 `play_audio` 和本地 TTS 设置。
主动请求单独声明允许静默；普通桌面提问不允许静默。Core 可在没有值得交流的话题时返回精确对象 `{"silence":true}`，该终态不进入历史、不朗读，也不计入未回应次数；是否开口由同一次回复生成决定，不额外调用分类器或 Planner。
模型选择静默后，至少从静默完成时起等待 `proactive_idle_seconds`；若当时满足双重空闲条件，退避间隔可延长此期限。其后键鼠活动会清除延长部分，但不会缩短静默的最低等待时间。
此功能仅在 Core 聊天模式有效，无核心独立模式不启动主动对话。

### 桌宠启动和日志

仓库根目录的 `launch_desktop_pet.bat` 是桌宠用户入口，`launch_sprite_pet.bat`
用于显式启动 Sprite。默认配置选择 Live2D，因此启动器会同步基础依赖、检测有效后端，
并只在选择 Live2D 时安装 provider extra。模型资源保留在
`NachoBot-Live2D-Adapter/resources`。
统一启动入口按配置选择桌宠或直播模式。启动器会同步依赖、
在后台启动透明窗口并确认 WebSocket 端口实际监听。实时日志写入本目录的 `logs`，
需要查看时可执行 `launch_desktop_pet.bat logs ALL`，也可用 `logs LIVE2D` 或 `logs CORE` 单独查看。
桌宠关闭（包括托盘退出、崩溃或进程被结束）后，隐藏的监督器会结束本次由桌宠启动的 Core。
如果 Core 在启动桌宠之前已经运行，桌宠会复用它，退出时不会关闭它。
启动失败也会清理本次启动的 Core；外部 Core 和其他适配器不受影响。

Live2D 日志固定打印在运行 `launch_desktop_pet.bat` 时的原始 Adapter 终端，不再另开 Live2D 日志窗口；关闭原终端或按 Ctrl+C 会结束对应桌宠会话。
在 `config.toml` 的 `[desktop_pet]` 中可额外显示本次启动的 Core 控制台，重启后生效：

```toml
show_core_console = false    # true：用同一日志查看器单独显示 Core 日志
```

Core 控制台默认关闭；关闭启动时显示的任一控制台会结束本次桌宠，并清理本次启动的 Core；启动前已存在的 Core 不会被终止。
桌宠退出时，随它打开的日志窗口也会退出。渲染与服务进程由监督器管理，同时持续向原 Adapter 终端输出日志，并写入适配器的 `logs`。
已有外部 Core 时不再另开没有有效输出的 Core 日志窗口。
Core 控制台在服务启动前打开，按本次启动的文件游标读取新增日志，不回放旧会话；日志每行刷新，空闲检查间隔为 20ms。
Core 查看器读取本适配器记录的 Core 日志；复用独立启动的 Core 时，需在其原控制台查看最新日志。
手动使用 `launch_desktop_pet.bat logs ALL` 同时显示两类日志，`logs LIVE2D` / `logs CORE` 单独显示对应日志。
启动器监控原 Adapter 终端和可选 Core 控制台的生命周期，关闭任意一个都会结束本次桌宠；手动 `logs` 查看器只负责查看。
日志窗口会绑定自己的控制台输出，不继承后台监督器的文件句柄；服务暂无日志时也会显示等待提示。
日志控制台直接显示原日志系统输出的颜色：Live2D 使用 Loguru，Core 沿用 structlog。
查看器不会添加来源前缀或将整行重新染色；`logs\live2d.log` 仍保留普通文件日志格式。
原 Adapter 终端会连续显示本次启动和运行日志；手动日志查看器先显示完整日志文件，再连续追加输出。
配置使用相对路径，因此移动整个仓库后
无需改盘符；使用模型时仍须遵守模型目录中的 Live2D 示例模型许可。

桌宠配置仅列出常用设置；缩放边界、空闲动作、超时与状态文件路径使用内置默认值，已有高级覆盖仍兼容。窗口位置与缩放保存在 `state/`，不会回写配置。

桌宠操作：

- 输入条、图标、历史和参数面板随模型缩放同步调整字号、间距与尺寸（保留可读下限，放大受屏幕工作区限制）；草稿、滚动和手动参数不会因缩放重建。

- 桌面聊天使用现代浅色浮层：白色圆角表面、冷灰分组背景、青绿强调色，输入条右侧为可点击的纯图标发送按钮，也支持 Enter。默认只显示桌宠；光标移到可交互的桌宠上时，无边框输入条显示在桌宠脚下，左侧参数图标和历史图标从桌宠旁滑出，分别打开模型参数和聊天记录。
- 历史面板只读、可滚动、可拖动，位置会记住；输入框只保留一个无边框输入区域，按 Enter 发送普通文字。输入框位于桌宠正下方，屏幕底边空间不足时会向上收进可见区域，不会翻到桌宠上方。
- 光标离开桌宠、输入框、两个按钮及已打开的面板后，整组界面会在短暂缓冲后隐藏；正在输入、输入框里有尚未发送的草稿且仍聚焦，或正在拖动历史/参数滑块时会保持显示。收起会保留草稿；按 Escape 会收起整组，并在光标离开桌宠前抑制重新显示。
- 双击桌宠会在光标仍位于桌宠时聚焦输入框；鼠标穿透或桌宠隐藏时不会触发悬停显示。聊天功能未启用时，悬停“设置”按钮仍可打开模型参数面板。
- 左键拖动桌宠窗口；右键打开与任务栏图标相同的菜单。
- `Shift + 左键` 拖动可调整人物在窗口内的位置，滚轮缩放人物；默认 `fit_to_window = true`，会在人物即将被固定透明视口裁剪前停止缩放。
  如确实使用自定义大画布，可关闭该限制，但需要自行保证模型不超出窗口。
- 点击桌宠左侧的“设置”按钮可查看模型实际提供的参数滑块及各自的最小值、最大值。滑块调整会为当前桌宠会话覆盖动画；重置单个参数组或全部参数后，会解除对应覆盖并恢复动画驱动，不修改模型资源或配置文件。
- 桌宠固定置顶显示；点击、双击、拖动桌宠及切换语音模式不激活桌宠窗口。主动点击文字输入区域时才进入键盘输入。
  系统托盘菜单可以显示/隐藏、切换鼠标穿透、复位位置或退出。独占全屏应用可能覆盖桌面窗口，游戏内叠加显示取决于游戏窗口模式。
- 窗口位置、缩放和开关状态会写入本目录的 `state`，不会改动模型资源。
- 最近 100 条双方消息保存在本地 `state\desktop_pet_chat_history.json`，重启桌宠后仍可从独立历史面板查看；Core 使用固定本机会话身份维持连续问答上下文。
- Local Host 和 Bilibili 只提供问题/回复元数据，`control_pipeline.py` 会调用 `action_adapter.py` 统一选择 canonical 动作：确认问题按回答点头/摇头、方向问题转向、普通疑问歪头、夸奖/开心身体晃动、害羞移开视线；模型实际动作组仍由本适配器的 `[actions]` 映射解析。
- NachoBot 后端可继续通过 `ws://127.0.0.1:8766` 发送动作、情绪、视线、说话和音频命令。

桌宠启动器写入本目录的 `logs\live2d.log`，日志窗口会持续读取这个结构化日志。
也可以通过同一个适配器运行 `live` 模式。
日志中会明确记录模型适配、窗口边界、双击聚焦、缩放封顶、TTS 音频大小与时长等验收信息。

本目录的 `config.toml` 是桌宠和直播模式共用的入口配置，默认使用 `desktop_pet`。
`launch_desktop_pet.bat desktop_pet` 会明确选择桌宠模式，
`launch_desktop_pet.bat live` 会明确选择直播模式。如果模型移动了，只需修改配置的
`model_path`；`character_name` 不填或留空时，优先读取所选模型对应的 `.vtube.json` / `.model3.json` 的 `Name`，
再使用模型目录名（`runtime` 等通用目录回退模型文件名）。标题与历史名称同步生成；手动设置仍优先，无需改 Python 代码。

输入框发送普通文字后，文字回复先显示，语音在后台合成并播放。语音语言默认中文（`tts_language = "zh"`）；悬停桌宠后可用语言按钮切换 `auto`、中文、日语或英语。Core transport 将整条原文和当前 `text_lang` 偏好发送到 Core 的多模态语音接口。文字显示和聊天历史仍保留 Core 回复原文。声音开关与默认语种由 `[desktop_pet.chat]` 中的 `play_audio` 和 `tts_language` 配置控制。

桌宠默认直接连接 `[desktop_pet.chat].core_url`，本机默认地址为 `ws://127.0.0.1:8000/ws`。
启动器检查本机 Core；需要鉴权时设置环境变量 `NACHOBOT_CORE_TOKEN`。
既有 HTTP 聊天桥仍可通过 `transport = "http"` 和 `backend_url` 使用。

启动开关（`NachoBot-Multimodal-Adapter/startup.toml`）：

```toml
[components]
core = true # false：纯本地独立桌宠，禁用所有后端功能
tts = true
perception = true
```

聊天配置仍在本项目的 `config.toml`：

```toml
[desktop_pet.chat]
enabled = true
transport = "core"
core_url = "ws://127.0.0.1:8000/ws"
core_reply_model_group = "" # 沿用 Core 当前模型，不强制使用其他模型组
play_audio = true
tts_url = "http://127.0.0.1:9880" # 兼容旧配置保留；Core transport 从 core_url 推导语音地址
tts_language = "zh"
```

在线 neural/Edge 语音已移除，不再安装在线语音依赖。Core transport 通过从 `core_url` 推导出的 HTTP(S) origin 使用 Core 的
`/api/multimodal/tts/stream`，要求版本 1 的 PCM 格式头；旧在线 provider 配置会明确报错。
上面 `[components]` 三个开关统一写在 `NachoBot-Multimodal-Adapter/startup.toml`，不再写入 Live2D 的 `config.toml`。
`core = false` 进入无核心独立模式：只保留模型显示、鼠标交互、托盘和参数面板；不连接已有 Core，
不启动 Core、TTS、perception，不创建聊天/语音客户端，也不采集屏幕或麦克风。`tts` / `perception` 即使为 `true` 也会被总开关覆盖。
重新设为 `core = true` 并重启可恢复聊天，原来的聊天与采集设置会保留，不改写配置文件。
BAT 先交给本适配器的 `scripts/launch_desktop_pet.py` 编排入口，按 `NachoBot-Multimodal-Adapter/startup.toml` 中的
`components.core`、`components.tts`、`components.perception` 选择后端服务，再启动桌宠。三个开关默认开启，可分别关闭。
Vox/TTS 与 perception 各有独立控制台和进程树，模型初始化日志在各自控制台逐条打印；重复启动复用已有服务或正在加载的服务。
Live2D provider 只负责渲染，不启动、停止或接管这些服务，也不读取它们的模型配置。
Core transport 的语音播放和策略查询连接 Core；`tts_url` 保留给本地 TTS 服务管理，不作为 Core 请求地址。视觉和语音输入通过 Core 的 perception API 调用已配置的感知服务。
`play_audio`、`vision`、`speech` 只控制桌宠使用对应功能，不控制模型服务生命周期。
服务日志在各自的控制台查看；退出桌宠会清理本次桌宠拉起的 Core、Vox/TTS 和 perception 进程树。
启动前已经运行或由其他入口拉起的服务仅复用，不纳入桌宠退出清理；启动失败和启动终端异常退出也会撤销本次服务归属。
关闭对应服务控制台会清理它自己的模型子进程。模型冷启动在后台继续，控制台的“started”仅表示进程已拉起，不表示模型已就绪。
`live` 模式和 `logs` 查看入口也不会拉起模型服务。

桌宠以固定 `local.live2d/host` 私聊身份接入 Core，使用桌面陪伴专用 prompt，沿用 Core 人格。
轻量直回只绕过 Planner，保留记忆、知识和按 Core 权限开放的工具/MCP。
回复通过版本化 `structured_reply` 消息段传输正文与表现控制，内外两层请求 ID 必须匹配；
历史和语音只使用正文，格式错误或过期回复不会作为控制 JSON 展示。
PCM 按实际采样率重采样，保留音高和时长，结尾音频排空后才停止口型。
本机模型生成慢于播放速度时仍可能产生间隔，应选择更快的本机模型。

```toml
[desktop_pet.perception]
vision = false
speech = false
push_to_talk_key = "F8"
push_to_talk_mode = "hold"
```

两个开关分别控制视觉上下文和语音输入，均要求 `transport = "core"`。启用语音输入后，底部输入条左侧改为纯麦克风图标。
按住模式下，按住录音、松开后通过 Core 的 `audio.transcribe.v1` 转写并自动发送，单次录音最长 30 秒。
录音期间会停止桌宠自己的语音播放。识别失败保留文字输入，不制造空消息。高级可选项 `microphone_device` 接受设备编号或名称。

`push_to_talk_key` 为全局说话绑定，仅在 `speech = true` 时生效，不需要聚焦桌宠。
`push_to_talk_mode` 可设为 `hold`（默认：按住录音、松开发送）或 `toggle`（按一下开启持续录音，再按一下关闭）。
悬停桌宠后，侧栏第三个纯图标按钮可切换模式：圆形按键代表按住模式，青绿拨动开关代表切换模式。切换仅影响当前会话，重启以配置为准。
两种模式同时适用于键盘绑定与输入条麦克风；切换模式取消当前录音与待发送转写。
持续模式保持麦克风开启，以约 0.8 秒停顿分句，经 Core 转写后逐句发送；长句按最多 15 秒分段，持续收音不受 30 秒限制。
松开按键或悬停工具栏自动收起均不会关闭持续录音，开启时麦克风与侧栏开关以红色显示。再次按键或点击麦克风可关闭，并丢弃未完成、排队及迟到的语音转写。
Escape、隐藏桌宠或退出也会关闭麦克风。桌宠自身语音播放期间及结束后的短暂余音阶段不识别，以免回声触发对话。
识别与发送队列均有容量限制，积压过多会关闭录音并提示；音频只在内存中处理，不保存到文件。
支持 `F1`～`F24`、字母、数字、`space`、`ctrl_r` 等单键，以及 `ctrl+alt+v` 等组合键；按住模式下，组合键任一键松开就结束录音。
留空 `""` 关闭键盘绑定，麦克风按钮仍可使用。Escape 保留为取消键，不能绑定为说话键。
启动时已经按住、按键重复、录音达时长上限或取消后继续按住，都不会重复开始录音，须松开后重新按下。
按住模式下，键盘与麦克风按钮各自结束自己发起的录音；持续模式下，两者均可关闭当前录音。键盘绑定不拦截其他应用收到的原按键。

启用视觉后采用 Bilibili 同款活动窗口边界采集：`mss` 截图、Pillow 兜底、最长边 1280 的 JPEG，经 Core
`image.describe.v1`（`vlm_fast`）生成摘要。默认每 30 秒刷新，截图只在内存中处理，不写入桌宠状态或日志。
输入框取得焦点时沿用最近的外部窗口；目标不可用、最小化或被排除时跳过采集，不扩大为全屏。
可选 `capture_interval_seconds`、`capture_active_window`、`excluded_exes`；默认排除 OBS。
摘要附带采集时间，过期或失败后不再使用；它作为独立视觉背景进入当前消息，不伪造用户发言，也不自行触发聊天。

感知请求使用 `core_url` 对应 HTTP 地址的 `/api/multimodal/perception`，沿用 `NACHOBOT_CORE_TOKEN`。
模型和本地/远程候选由 Core 的 `model_task_config.vlm_fast`、`voice` 配置选择；FULL 使用本地候选时须已有就绪的 perception 服务，
Live2D Adapter 不负责拉起 perception 或 TTS 服务。配置开启感知不代表设备或模型已经验收成功；视觉异常写入日志，语音输入异常在界面提示。

原桌宠目录的聊天历史、聊天窗位置和人物状态已迁入 `state`，原日志保留在
`logs\desktop-pet-merge-*`。迁移前的两份配置和校验记录保留在
`state\migration-backups\desktop-pet-merge-*`；旧版放在本目录根部的状态文件也保留原样。
`state` 和 `logs` 均为本地运行数据，不提交到 Git。

### Docker 边界

透明 Live2D 桌面窗口和 Tk 聊天窗必须运行在 Windows 宿主机，不能把 `desktop_pet` 模式
放进普通 Linux 容器。项目 Compose 仅运行无 GUI 的 WebSocket `live` 模式，并以仓库根目录
为 build context，同时复制宿主、Live2D 组合项目和独立 provider；容器在组合项目自己的
`.venv` 中安装锁定依赖，模型资源从 `NachoBot-Live2D-Adapter/resources` 挂载。启动 Compose
的命令从仓库根目录执行：

```bat
docker network create nacho_bot
docker compose -f NachoBot-Desktop-Pet-Adapter/docker-compose.yml up -d
```

桌宠采用混合部署：宿主机执行 `launch_desktop_pet.bat`，后台服务可使用仓库根目录的 Core
Compose，并将宿主机端口映射到 `8000`；使用兼容 HTTP 聊天桥时另映射 `8789`。本机语音服务默认端口为 `9880`，由宿主机播放音频。

### 通用适配器

将 `config.toml` 中的 `mode` 改为 `live` 后，仍双击同一个入口：

```text
launch_desktop_pet.bat
```

建议启动顺序：

1. 启动 `NachoBot-Desktop-Pet-Adapter`。
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

当前协议版本：`1.1`（主版本仍为 `1`，因此与既有 `1.x` 客户端保持兼容）。

### 命令信封

```json
{
  "type": "avatar.command",
  "version": "1.1",
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
- `prepare_reply`：payload 为原始回复文本（通常使用 `reply` 字段）。响应事件
  `reply_prepared` 会复用请求的 `request_id`，payload 只包含规范化的
  `reply`、`web_search`、`search_query` 和不透明 `control_id`；`control_id`
  稳定地等于 prepare 请求的 `request_id`。
- `apply_control`：payload 为 `control_id`。响应事件 `control_applied` 会返回
  `applied`、`already_applied` 或明确的 `unknown`/过期状态。每个控制最多向
  渲染队列入队一次，重复请求不会重复触发情绪或动作。
- `ping`
- `shutdown`

### 交互信封

```json
{
  "type": "avatar.interaction",
  "version": "1.1",
  "event": "ready",
  "payload": {
    "running": true,
    "protocol_version": "1.1",
    "capabilities": {
      "prepare_reply": true,
      "apply_control": true
    }
  }
}
```

支持的交互事件：

- `ready`
- `click`
- `poke`
- `pong`
- `reply_prepared`
- `control_applied`
- `error`

协议只保证主版本兼容。客户端和服务端的 major version 不一致时，服务端会返回协议错误。

`prepare_reply` 负责识别 plain text、JSON 或 fenced JSON，校验允许的 emotion，
并把既有中文动作标签映射为 canonical action ID；`IDLE`/`GENERAL` 继续忽略。
暂存控制按 WebSocket 客户端隔离，并同时受 TTL 与最大数量限制。客户端断开时，
该客户端的暂存控制会被丢弃。控制只在 Bilibili 发送前或首段 TTS 音频就绪时由
`apply_control` 触发；解析阶段不会改变模型状态。

## 交互行为

- 鼠标左键拖动模型。
- 鼠标右键拖动透明窗口。
- 鼠标滚轮缩放模型。
- 鼠标侧键 6 或 7 触发 `click`；通过冷却检查后额外触发 `poke`。
- `track_mouse = true` 时持续跟踪鼠标视线。
- `speaking` 命令控制嘴部参数动画。

### 桌面自主互动

Windows 桌宠模式可读取可见窗口、前台窗口和桌面图标位置，并通过移动桌宠视口
靠近窗口或图标。移动计划只输出 `approach_window`、`approach_icon`、`recover` 等
语义意图，不会把意图当成 Live2D 动作名。当前模型没有攀爬、搬运或点击动画，
因此默认只移动视口，不会用 `FlickUp`、`Tap` 或其他无关动作冒充攀爬。只有在模型
确实提供对应动作组时，才可通过 `[desktop_pet.presentations]` 显式配置展示映射；
映射不存在或动作组不可用时会跳过动画，位置移动仍可继续。

路径规划使用整个渲染视口作为保守边界；没有测量过角色轮廓。`foot_anchor_x`/
`foot_anchor_y` 是该视口内的近似锚点，不代表精确的角色边界。窗口边缘空间不足或
被遮挡时，角色不会移动到不可达的位置。

配置位于 `[desktop_pet.interactions]`。当前示例启用自主互动，空闲 90 秒后才
选择动作；普通未最大化窗口和图标的自动移动上限各为 12 像素，并可从托盘
菜单恢复原位置：

```toml
[desktop_pet.interactions]
enabled = true
idle_interval_seconds = 90
scene_interval_seconds = 1.5
window_approach = true
icon_approach = true
follow_foreground_window = true
move_windows = true
move_icons = true
gui_inspection = true
gui_actions = true
max_window_shift_px = 12
max_icon_shift_px = 12
foot_anchor_x = 0.5
foot_anchor_y = 0.9
```

桌面互动菜单提供总开关，以及窗口跟随、靠近图标、窗口/图标轻推、UIA 检查和
UIA 动作的单独开关。拖动桌宠、用户活动、隐藏窗口或检测到全屏应用时会暂停
自主外部操作；前台窗口移动时，角色仍可保持跟随。UIA 只通过 Windows
Automation 控件模式（Invoke、Toggle、Expand）操作控件，不发送盲目坐标点击。

要选择 UIA 目标，先在托盘菜单运行“扫描前台控件”，再从“允许的控件”中明确
勾选目标；选择会保存到桌宠状态目录的 `desktop_gui_targets.json`。Windows 托盘
菜单会临时取得前台焦点，因此扫描使用最近一次采样到的外部前台窗口，并在读取
前后验证窗口身份。动作只对配置的前台窗口和精确控件身份有效，执行前会重新
验证目标；操作时目标仍须处于前台，桌宠不会替用户切换窗口。名称看起来可能会
删除、发送、购买、付款、转账或关闭的控件会被拒绝；不要允许会产生其他重要
外部影响的控件。目标也可在配置文件中通过 `gui_targets` 指定。

窗口场景、Shell 图标枚举和 UIA 请求在有超时的后台线程处理，不占用渲染循环。
托盘中的“恢复窗口与图标位置”会还原本次会话记录过的位置；正常退出也会尝试
还原。只有普通、未最大化的窗口可被轻推，图标仅通过 Windows Shell 的
`IFolderView` 接口读取或移动。桌面图标移动依赖 Windows 桌面使用传统图标视图；
若 Shell 不提供该视图，图标互动会保持不可用而不影响渲染。

## 组件边界

- 桌宠宿主仅负责通用运行时；Live2D provider 和模型资源位于独立项目。
- Bilibili Adapter 仅使用远程 WebSocket 控制器。
- Bilibili Adapter 不再为了 Live2D 构造 NachoBot `MessageRecv` 或模拟消息流。
- 主运行路径不再导入旧本地控制器、动作管理器、情绪管理器或渲染器桥接模块。

## 故障排查

### `import live2d.v3` 失败

通过配置已选择 `backend = "live2d"` 的 `launch_desktop_pet.bat` 启动，或在项目目录执行 `uv run --project renderers/live2d --locked python -m desktop_pet_adapter --config config.toml --mode live`。Sprite 模式无需 sibling provider 项目。

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
