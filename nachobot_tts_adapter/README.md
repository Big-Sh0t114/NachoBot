# Maimbot TTS 适配器

基于多个服务商的文本转语音(TTS)适配器，支持流式和非流式语音合成。

## 功能特性

- 支持多种provider配置，可以动态启用不同的provider
- 支持流式和非流式语音合成
- 支持多平台预设配置
- 基于 GPT-SoVITS 等provider实现高质量语音合成
- 可配置的语音参数（语速、采样步数等）
- 支持参考音频设置
- 灵活的消息路由系统

## 安装说明

1. 克隆项目仓库：

```bash
git clone https://github.com/tcmofashi/maimbot_tts_adapter
cd maimbot_tts_adapter
```

2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 安装 GPT-SoVITS（或其他provider）：

自行配置GPT-SoVITS，配置好相关环境后启动api：

```bash
# 按照GPT-SoVITS项目教程安装依赖...

python api_v2.py 
```

## 基本配置说明

配置文件位于 `configs/base.toml`，包含以下配置项：

### Server 配置
这个配置标识的是给上游的Adapter（比如MaiBot-Napcat-Adapter）提供的服务端
```toml
[server]
host = "127.0.0.1"
port = 8070
```
### Route 配置
这个配置标识的是给下游的MaiBot主体的连接
```toml
[routes]
qq = "http://127.0.0.1:8090/ws" # 或者nonebot-qq
# nonebot-qq = "default"
```
### Probability 配置
这部分决定麦麦选取语音的概率
```toml
[probability]
voice_probability = 0.2 # 使用语音的概率
```
### EnabledTTS 配置
这部分标识的是选择启用的插件名称，其名称应该与插件的文件夹名称一至（即python模块名）
```toml
[enabled_tts] # 启用的TTS模块，请与各插件的目录名称一致
enabled = ["GPT_Sovits"]
```
### TTSBaseConfig 配置
这部分是TTS的通用配置
```toml
[tts_base_config]
stream_mode = false    # 是否启用流式输出
post_process = false # 是否启用后处理（现阶段无效）
```

## 内置 GPT-SOVITS 插件 TTS 配置

```toml
[tts]
host = "localhost"                        # 根据GPT-SoVITS的api配置填写
port = 9880
ref_audio_path = "path/to/reference.wav"  # 参考音频路径
prompt_text = "示例文本"                   # 参考文本
text_language = "zh"                      # 文本语言
prompt_language = "zh"                    # 提示语言
# 其他 TTS 相关参数...

[tts.models]
gpt_model = "path/to/gpt/model"          # GPT 模型路径
sovits_model = "path/to/sovits/model"    # SoVITS 模型路径

[tts.models.presets]                      # 预设配置
[tts.models.presets.default]              # 默认预设
name = "默认"
ref_audio = "path/to/default/ref.wav"
prompt_text = "默认提示文本"

[pipeline]
default_preset = "default"                # 默认使用的预设名称

[pipeline.platform_presets]               # 平台特定的预设配置
platform1 = "preset1"
platform2 = "preset2"
```

## 内置其他服务商配置
请参考[官方文档](https://docs.mai-mai.org/manual/adapters/tts/)使用。

## 使用方法

1. 启动服务：

```bash
python maimbot_pipeline.py
```

2. 将adapter的目标路由填写为本项目服务器配置，将本项目的路由配置填写为maimbot core的服务器配置

## 注意事项

- 确保已正确配置 GPT-SoVITS 模型路径等各项参数
- 参考音频和提示文本对语音质量有重要影响
- 流式模式适合长文本实时合成，非流式模式适合短文本高质量合成

## 开源协议

本项目遵循 MIT 协议开源。