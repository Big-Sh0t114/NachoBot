# 日记插件(Diary Plugin)

让麦麦能够回忆和记录每一天的聊天，生成个性化的日记内容。

- 当前插件的mian分支为**最新版**，仅适配maibot  v0.10.2  版本。
- **旧版本插件可从右侧Releases页面获取**，请根据MaiBot版本选择对应插件版本。





## ⚠️ 重要注意事项

- **Bot配置要求**：请确保MaiBot配置文件 `bot_config.toml` 的`qq_account`(bot的qq号)填写正确，该QQ号用于QQ空间发布和消息查询功能，配置错误会导致功能异常。
- **Bot昵称设置**：`nickname`(bot的名字)可以自由更换，不影响插件功能。





## 安装依赖

在使用插件前，请先安装必要的依赖：

- 注意，要在麦麦主程序的虚拟环境下执行

```bash
cd plugins/diary_plugin
pip install -r requirements.txt
```

或手动安装：
```bash
pip install httpx pytz openai
```





## 配置文件管理

**重要说明**：插件现在支持自动生成配置文件，无需手动管理。

### 配置文件生成流程

1. **首次安装/更新插件**：系统自动根据config_schema生成config.toml文件
2. **配置文件已存在**：系统保留现有配置，不会覆盖
3. **需要恢复默认配置**：删除config.toml文件，重启插件后会自动重新生成






## 使用方法

### 日记命令

**注意：只有管理员才能使用所有命令**

- `view` 命令除外，所有人都可以使用，方便查看日记内容。
- `generate` 的特点：**不与黑白名单联动**
  - 0、当自定义模型启用时，使用自定义模型生成日记（不启用自定义模型时则使用默认回复模型且50k截断）。
  - 1、在群聊时使用则只生成当前群聊的日记，其它群聊不再参与日记生成。
  - 2、在私聊时使用则强制全部活跃群聊，所有符合条件的群聊均参与日记生成。
  - 3、使用50k token强制截断保护，确保生成稳定。
  - 4、主要用于验证整体流程和测试功能。


**非管理员使用指令的响应**：
- **群聊内**：无任何响应消息（完全静默）
- **私聊内**：回复"❌ 您没有权限使用此命令。"

```bash
# 生成日记（自动发布到QQ空间）（管理员专用）
/diary generate              # 生成今天的日记并发布到QQ空间
/diary generate 2025-08-24   # 生成指定日期的日记并发布

# 日记概览（管理员专用）
/diary list                  # 显示日记概览（统计 + 最近10篇）
/diary list 2025-08-24       # 显示指定日期的日记概况
/diary list all              # 显示详细统计和趋势分析

# 调试信息（管理员专用）
/diary debug                 # 显示今天的Bot消息读取调试信息
/diary debug 2025-08-24      # 显示指定日期的调试信息

# 查看日记（所有用户可用）
/diary view                  # 查看当天日记列表
/diary view 2025-08-24       # 查看指定日期的日记列表
/diary view 2025-08-24 1     # 查看指定日期的第1条日记内容

# 帮助信息（所有用户可用）
/diary help                  # 显示所有可用命令
```



## 配置说明

### 插件基础配置 [plugin]

```toml
[plugin]
enabled = true
config_version = "2.0.0"
admin_qqs = []
```

- `enabled`：控制插件是否启用
- `config_version`：配置文件版本
- `admin_qqs`：管理员QQ号列表，只有列表中的QQ号才能使用日记命令，空列表表示无人有权限





### 日记生成配置 [diary_generation]

```toml
[diary_generation]
min_message_count = 3
min_messages_per_chat = 3
enable_emotion_analysis = true
```

- `min_message_count`：生成日记所需的最少消息总数，所有聊天合计少于此值时不生成日记
- `min_messages_per_chat`：每个聊天的最少消息数量，单个群组聊天消息数少于此值时跳过该群组聊天
- `enable_emotion_analysis`：是否根据聊天情感生成天气，关闭时随机选择天气

**消息过滤机制**：
1. 先按 `min_messages_per_chat` 过滤掉消息数量不足的单个聊天
2. 再检查剩余群组消息的总数是否满足 `min_message_count`
3. 两个条件都满足才会生成日记





### QQ空间发布配置 [qzone_publishing]

```toml
[qzone_publishing]
qzone_word_count = 500
napcat_host = "127.0.0.1"
napcat_port = "9998"
```

- `qzone_word_count`：QQ空间说说的字数限制，范围20-8000字，超过会自动截断
- `napcat_host`：Napcat服务地址
- `napcat_port`：Napcat服务端口

**Napcat配置要求**：
1. 在napcat的webui中新建**http服务器**
2. host填**127.0.0.1**，port填**9998**
3. 启用**CORS和Websocket**



**Docker用户特别说明**：

> [!WARNING]
> **Docker环境限制**：目前存在问题，发送空间相关无法正常运行。

如果您使用Docker部署：

**配置方法**：
1. 将napcat创建的`http server`中的`host`改为`core`或者`0.0.0.0（不推荐）`
2. 将本插件的config.toml中的`napcat_host`值写为`napcat`
3. 确保Docker网络配置正确，napcat容器能与MaiBot容器通信

**已知限制**：
- ⚠️ **QQ空间发布功能异常**：Docker环境下无法正常获取QQ空间cookies。
- ⚠️ **定时任务功能异常**：定时是直接将日记发送到空间的，所以也无法正常工作。
- ✅ **日记生成功能完全正常**：不影响日记内容生成和本地存储（可以通过指令生成本地日记）。

**可以尝试使用空间插件提供的其它获取cookies的方式去手动获取、生成。（在文档最后有空间插件地址）**





### 自定义模型配置 [custom_model]

```toml
[custom_model]
use_custom_model = false
api_url = "http://rinkoai.com/v1"
api_key = "sk-your-rinko-key-here"
model_name = "Pro/deepseek-ai/DeepSeek-V3"
temperature = 0.7
max_context_tokens = 256
api_timeout = 300
```

- `use_custom_model`：是否使用自定义模型，false时使用系统默认的首要回复模型
- `api_url`：API服务地址，必须兼容OpenAI格式，**使用基础URL格式**（不包含/chat/completions）
- `api_key`：API密钥
- `model_name`：模型名称
- `temperature`：生成温度，范围0.0-1.0，推荐0.7
- `max_context_tokens`：模型上下文长度（单位：k），填写模型真实上限
- `api_timeout`：API调用超时时间（秒），大量聊天记录时建议设置更长时间，默认300秒

**API URL 格式说明**：
```toml
# ✅ 正确格式（基础URL）
api_url = "http://rinkoai.com/v1"
api_url = "https://api.siliconflow.cn/v1"

# ❌ 错误格式（包含具体端点）
api_url = "http://rinkoai.com/v1/chat/completions"
```

**重要限制**：

- 仅支持OpenAI API格式的模型服务
- 不支持Google Gemini、Anthropic Claude等原生格式
- 请确保设置的上下文长度不超过模型真实上限，否则会出现400错误





### 定时任务配置 [schedule]

```toml
[schedule]
schedule_time = "23:30"
timezone = "Asia/Shanghai"
filter_mode = "whitelist"
target_chats = []
```

- `schedule_time`：每日生成日记的时间，HH:MM格式

- `timezone`：时区设置，支持Windows/Linux/Mac等所有系统
  - 常用时区：Asia/Shanghai (中国标准时间)、Asia/Tokyo (日本标准时间)、America/New_York (美国东部时间)、America/Los_Angeles (美国西部时间)、Europe/London (英国时间)、UTC (协调世界时)

- `filter_mode`：过滤模式，可选值whitelist(白名单)或blacklist(黑名单)

- `target_chats`：目标列表，格式["group:群号", "private:用户qq号"]

> [!NOTE]
>
> **过滤模式说明**：

- **白名单模式**：只处理target_chats中指定的聊天，空列表时禁用定时任务
- **黑名单模式**：处理除target_chats外的所有聊天，空列表时处理所有聊天

**配置示例**：

```toml
# 只处理特定聊天
filter_mode = "whitelist"
target_chats = ["group:123456789", "private:987654321"]

# 排除特定聊天
filter_mode = "blacklist"
target_chats = ["group:999999"]
```





## 日志说明

插件会在不同日志级别显示相应信息：

### INFO级别（推荐）
- 插件启动时的配置状态
- 管理员配置情况
- 过滤模式和定时任务状态
- 模型配置（默认/自定义）
- 模型调用信息（使用的具体模型）
- 定时任务启动/禁用状态
- 日记生成和QQ空间发布结果

### DEBUG级别（详细调试）
- 配置读取详情
- Token截断过程
- 消息过滤统计
- 聊天ID映射过程
- 其他技术细节

**建议**：日常使用INFO级别即可了解插件运行状态，遇到问题时切换到DEBUG级别查看详细信息。





## 故障排除

**Q: 命令提示"权限不足"**

A: 确保QQ号已添加到admin_qqs配置中

**Q: 日记生成失败，提示"消息数量不足"**

A: 检查min_message_count和min_messages_per_chat配置，或当天确实聊天较少

**Q: QQ空间发布失败**

A: 检查Napcat服务是否运行，端口配置是否正确

**Q: 自定义模型返回400错误**

A: 上下文长度超限，请降低max_context_tokens配置值

**Q: 自定义模型调用失败**

A: 检查API地址、密钥和模型名称是否正确，确认服务商支持OpenAI格式，确保api_url使用基础URL格式

**Q: 定时任务没有执行**

A: 检查filter_mode和target_chats配置，白名单空列表会禁用定时任务

**Q: API调用超时，提示"超时错误"**

A: 聊天记录过多导致处理时间过长，建议：

1. 启用自定义模型：`use_custom_model = true`
2. 增加超时时间：`api_timeout = 600`
3. 减少上下文长度：`max_context_tokens = 128`
4. 或调整MaiBot全局超时：config/model_config.toml中timeout改为180





## 🙏 鸣谢

部分重要代码来自空间插件：https://github.com/internetsb/Maizone

感谢 [internetsb（空间插件作者）](https://github.com/internetsb) 、[何夕](https://github.com/Heximiao) 提供的帮助。