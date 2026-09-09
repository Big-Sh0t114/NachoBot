# 星语主播 3D 资源目录

将 VRoid Studio 导出的原创 VRM 1.0 文件命名为：

```text
xingyu-host-v1.vrm
```

并放在本目录。随后将 `config.toml` 的 `[vrm].enabled` 改为 `true` 并重启本机主播控制台。

可选舞蹈动作文件放在 `motions/`：

```text
motions/cute.vrma
motions/energetic.vrma
motions/kpop.vrma
motions/hiphop.vrma
motions/shuffle.vrma
motions/elegant.vrma
```

动作加载顺序如下：

1. `motions/<舞种>.vrma`：用户自行提供的 VRMA，优先级最高。
2. `motions/aist/<舞种>.t2.json`：项目附带的 AIST++ 60 FPS 真人舞蹈动作。
3. 内置关键帧编舞：前两项缺失时的离线兜底。

真人动捕会驱动躯干、肩、肘、髋、膝和脚；手指层会额外驱动模型的 30 个手指骨骼，形成放松、张开、握拳和指向等手势。控制台的“握拳/合掌/扶腿”是四段式动作组合，用来直接检查动作准备、保持和收势。

控制台的“自动串舞”会每 12 秒轮换六种真人动捕舞蹈和一套动作组合。角色模型与动作文件相互独立，不需要重新导出 VRM。

项目附带动作的来源、许可证、署名和 SHA-256 见 `motions/THIRD_PARTY_NOTICES.md`。自行导入时只使用拥有直播展示许可的原创 VRM 与动作资源。
