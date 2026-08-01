# 视觉调用策略

视觉 prompt 是代码的一部分，不写入 TOML 等用户配置文件。适配器可通过
`BaseMessageInfo.additional_config.visual_policy` 携带代码中定义的场景 prompt
和可配置的单次推理参数，Core 负责校验、执行 VLM 和缓存。

QQ 是明确的例外：Napcat 与 Core 耦合，只发送固定的 `qq-core-v1` profile
以及推理参数；图片、静态/动态表情和视频 prompt 均由 Core 的
`src/chat/utils/visual_policy.py` 维护。即使 QQ 消息中意外携带 `prompt`，Core
也会忽略它，避免适配器或旧配置覆盖 QQ prompt。

QQ 消息示例：

```json
{
  "visual_policy": {
    "version": 1,
    "profile": "qq-core-v1",
    "image": {
      "temperature": 0.1,
      "max_tokens": 220,
      "extra_params": {
        "enable_thinking": false
      }
    },
    "emoji": {
      "temperature": 0.2,
      "max_tokens": 180,
      "extra_params": {
        "enable_thinking": false
      }
    },
    "video": {
      "temperature": 0.1,
      "max_tokens": 280,
      "extra_params": {
        "enable_thinking": false
      }
    }
  }
}
```

其他适配器可以在自身代码中定义 prompt，并在构造 `visual_policy` 时写入消息
元数据，但配置解析器不得从 TOML 读取 prompt。Provider、API Key 和模型注册
仍属于基础设施，可保留在 `config/model_config.toml`。

Core 会把最终解析出的 prompt、token 预算、温度和 `extra_params` 一起纳入缓存
指纹。修改代码 prompt 或参数只会失效对应场景的缓存，无需手动清库或修改
profile 版本。

当前归属：

- QQ/Napcat prompt：Core `src/chat/utils/visual_policy.py`
- Koishi 图片 prompt：`NachoBot-Koishi-Adapter/visual_policy.py`
- Bilibili 私信与直播画面 prompt：`NachoBot-Bilibili-Adapter/bili_src/visual_policy.py`
- DiscordVC、UniversalVC：没有视觉输入，不定义视觉策略
