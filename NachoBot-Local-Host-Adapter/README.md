# NachoBot 本机 AI 主播控制台

这是“不接入抖音数据”的第一阶段：在本机手动输入直播话题或台词，驱动 NachoBot、字幕、TTS 和可选 Live2D。它不会登录抖音、读取弹幕、礼物或私信，也不需要抖音开发者账号。

## 能做什么

- 在浏览器控制台输入话题，NachoBot 生成适合口播的回复。
- 直接播报一段已写好的台词，方便开场、转场和收尾。
- 把当前字幕写入一个 UTF-8 文本文件，可供 OBS “文本（GDI+）→ 从文件读取”使用。
- 可选驱动独立 `NachoBot-Live2D-Adapter` 的说话状态、情绪和动作。
- 可选调用 `NachoBot-Multimodal-Adapter` 的 TTS 并在本机播放。
- 使用 VRM 全身骨骼播放软萌、元气、K-pop、嘻哈、曳步和优雅舞蹈；“自动串舞”可连续轮换舞种。
- `models/motions/anyadance` 中存在同名 AnyaDance solved JSON 时优先播放；其次使用同名 `.vrma`，再其次使用附带的 AIST++ 60 FPS 真人动捕舞段，最后才回退到内置关键帧编舞。
- AnyaDance solved JSON 由 Blender + MMD Tools 对 `.vmd + .pmx/.pmd` 做离线 FK/IK 求解后生成，运行时按源模型 rest pose、髋部锚点和目标 VRM 身高重定向到双臂/双腿 IK；这样比把 MMD 源骨骼旋转直接套到 VRM 上稳定。
- 模型的 30 个手指骨骼已加入手势层，可在舞段中切换放松、张开、握拳和指向；“动作组合秀”包含握拳蓄力、双手合掌、扶腿下沉和展开谢幕四段完整动作。
- 每个舞段都包含“收臂换气”，待机时双臂自然垂在身体两侧，不会停留在 VRM 默认 T-Pose。
- 直播页使用 `assets/immersive-dance-stage-v1.png` 作为全身舞台，左侧保留字幕空间、右侧保留动作空间。

## 运行顺序

1. 启动 `launchbot.bat`，让 NachoBot Core（默认 `127.0.0.1:8000`）和已配置的 TTS 服务运行。
2. 需要虚拟形象时，启动 `NachoBot-Live2D-Adapter/launch_live2d.bat`，并在本项目 `config.toml` 中设定 `live2d.enabled = true`。
3. 双击根目录 `launch_local_host.bat`。首次启动会从模板创建 `config.toml`，并在浏览器打开 `http://127.0.0.1:8789`。
4. 在控制台输入一段直播话题，点击“AI 生成并播报”。

`launch_local_host.bat` 会等待 VoxCPM2 模型与本地 TTS 桥接全部健康后才启动控制台。浏览器系统朗读默认永久关闭；本地模型异常时保持静音，不会切回机械音色。

## 视频动作模仿的实现边界

可以增加“导入视频 → 提取人体关键点 → 平滑和脚底接触修正 → 重定向到 VRM 骨骼 → 导出/播放 VRMA”的本地处理链路。清晰、固定机位、全身无遮挡、单人视频最适合；单目视频缺少可靠深度，遮挡、转身、宽松衣物、手指和快速运动会降低还原度。因此不能对任意视频承诺固定 80%–90%，必须用逐帧关节角误差和画面重投影相似度验收。

## 导入 AnyaDance / MMD 舞蹈

当前控制台消费 AnyaDance 的 `anyadance_mmd_solved` JSON，不需要运行 SteamVR。准备 AnyaDance 仓库中的 `scripts/blender_export_mmd.py`、Blender、MMD Tools、一个 `.pmx/.pmd` 模型和对应的 `.vmd` 舞蹈后执行：

```powershell
blender --background --python scripts/blender_export_mmd.py -- `
  --model .\model.pmx --vmd .\dance.vmd `
  --output .\cute.json --fps 60
```

把输出文件复制为 `NachoBot-Local-Host-Adapter/models/motions/anyadance/cute.json`（可用 `cute`、`energetic`、`kpop`、`hiphop`、`shuffle`、`elegant` 之一命名），刷新舞台即可优先播放。动作和模型必须拥有可用于直播展示的授权；AnyaDance 的 solved JSON 与其源模型仍分别受各自许可约束。

## OBS 字幕

默认字幕文件是：

```text
NachoBot-Local-Host-Adapter/runtime/current_subtitle.txt
```

OBS 中添加“文本（GDI+）”来源，勾选“从文件读取”，然后选择这个文件。若 OBS 与控制台在同一台电脑，不需要域名、隧道或公网回调。

## 重要边界

- 这是主播手动触发的本机工具，不会自动回复真实抖音弹幕。
- 若 TTS 尚未配置，将保留字幕输出；可将 `tts.enabled` 设为 `false`。
- 之后若需要“观众评论自动触发 AI”，再单独接入抖音官方直播互动数据能力。
