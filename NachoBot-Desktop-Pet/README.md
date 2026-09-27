# NachoBot Desktop Pet

这里是 NachoBot 的独立桌宠前端，和 `NachoBot-Live2D-Adapter` 同级。

桌宠目录负责：

- 桌宠专用配置、启动入口、日志窗口和运行状态；
- 独立聊天窗、位置记忆和历史记录；
- 启动 Core、Local Host、VoxCPM2，并检查真实可用状态；
- 通过稳定的 Live2D WebSocket 协议驱动当前角色。

`NachoBot-Live2D-Adapter` 现在只作为可替换的 Live2D 渲染后端。桌宠不再以
Live2D 目录作为自己的工作目录；模型资源默认复用同级 Live2D 目录中的资源，
所以视觉效果和原来的桌宠保持一致。

## 启动

在仓库根目录执行：

```powershell
# 将当前目录切换到 NachoBot 仓库根目录
cd <repo-root>
.\NachoBot-Desktop-Pet\launch_desktop_pet.bat
```

启动器会：

1. 同步 Live2D 后端依赖；
2. 将旧版 Live2D 目录中的桌宠位置、聊天位置和历史记录迁移到本目录的 `state`；
3. 打开独立实时日志窗口；
4. 启动桌宠、聊天桥和语音服务；
5. 只在 WebSocket 端口真实监听且进程稳定后报告成功。

VoxCPM2 默认从仓库根目录下的 `VoxCPM` 目录加载。若模型安装在其他位置，启动前设置
`$env:NACHOBOT_VOXCPM_ROOT = "D:\AI\VoxCPM"`，或直接执行
`launch_local_neural_tts.ps1 -VoxRoot "D:\AI\VoxCPM"`。

关闭日志窗口不会退出桌宠。日志和桌宠状态都保存在本目录，不会污染 Live2D
适配器的运行目录。

## 配置边界

- `config.toml` 是桌宠的唯一入口配置；
- `renderer.model_path` 可以指向同级任意 Live2D 模型；
- `desktop_pet.chat.follow_pet = false` 时聊天窗保持独立；
- `fit_to_window = true` 时放大人物不会超出透明视口；
- 如果以后接入其他渲染器，只需实现同一 WebSocket 协议，不需要重写桌宠聊天和启动层。

原来的 `NachoBot-Live2D-Adapter\launch_desktop_pet.bat` 仍保留为兼容入口，
但会自动转发到本目录。直播模式仍使用 `NachoBot-Live2D-Adapter\launch_live2d.bat`。

## Docker

透明桌宠窗口必须运行在 Windows 宿主机；Core、Local Host 和 TTS 可以继续使用
Docker 或宿主机进程。不要把桌宠 GUI 强行放入普通 Linux 容器。
