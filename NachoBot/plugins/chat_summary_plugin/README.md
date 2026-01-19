# 聊天记录总结插件 (Chat Summary Plugin)

## 功能介绍

智能分析QQ群聊天记录并生成总结，支持精美图片输出。采用AI驱动，根据机器人人设生成个性化总结内容。

### 核心特性

- **群聊整体总结** - 分析群聊整体聊天内容，生成连贯的总结报告
- **单个用户总结** - 针对特定用户的聊天内容进行分析和总结
- **时间范围支持** - 支持指定日期（YYYY-MM-DD）的聊天记录，省略日期默认今天
- **智能群友称号** - 基于聊天行为分析，为群友生成有趣的称号
- **群聊金句提取** - 从聊天记录中提取精彩语录（群圣经）
- **炫压抑指数** - AI分析群友发言风格，生成压抑指数评分和文言文评价
- **精美图片输出** - 自动生成包含统计信息、装饰元素的精美总结图片
- **每日自动总结** - 支持每天定时自动生成群聊总结
- **智能降级机制** - 图片生成失败时自动降级为文本输出
- **时区支持** - 支持自定义时区配置，适应不同地区
- **发言分布统计** - 24小时发言活跃度分布图表

## 依赖要求

### Python 依赖

- **Pillow** (>=8.0.0, 必需) - 用于生成总结图片
- **pytz** (>=2021.1, 可选) - 用于时区支持（自动总结功能建议安装）

### MaiBot 要求

- **最低版本**: 0.11.0

### 系统字体

插件需要系统安装中文字体才能生成图片。大多数 Linux 发行版已预装中文字体，如未安装请参考下方安装命令。

#### Debian/Ubuntu 系统

```bash
# 【推荐】安装文泉驿正黑字体（本插件作者使用，轻量级且显示效果好）
sudo apt-get update
sudo apt-get install fonts-wqy-zenhei

# 可选：同时安装文泉驿微米黑（更细腻）
sudo apt-get install fonts-wqy-microhei

# 备选方案：Noto CJK 字体（Google 出品，体积较大）
sudo apt-get install fonts-noto-cjk

# 备选方案：Droid 字体（Android 默认字体）
sudo apt-get install fonts-droid-fallback
```

#### 其他 Linux 发行版

```bash
# CentOS/RHEL（推荐：文泉驿正黑）
sudo yum install wqy-zenhei-fonts

# Arch Linux（推荐：文泉驿正黑）
sudo pacman -S wqy-zenhei

# Fedora（推荐：文泉驿正黑）
sudo dnf install wqy-zenhei-fonts
```

#### Windows 系统

Windows 10/11 默认已安装微软雅黑字体（`msyh.ttc`），插件可直接使用。如果因某些原因缺失，可以：

**方法 1：从其他 Windows 系统复制**

1. 从正常的 Windows 系统复制 `C:\Windows\Fonts\msyh.ttc`
2. 粘贴到目标系统的 `C:\Windows\Fonts\` 目录
3. 右键点击字体文件，选择"安装"

**方法 2：下载开源中文字体**

下载并安装以下任一字体：

- **思源黑体**（Noto Sans CJK / Source Han Sans）
  - 下载地址：https://github.com/adobe-fonts/source-han-sans/releases
  - 下载 `SourceHanSansCN.zip`，解压后双击 `.otf` 文件安装

- **文泉驿微米黑**
  - 下载地址：https://sourceforge.net/projects/wqy/files/wqy-microhei/
  - 下载 `.ttf` 文件，双击安装

**安装后：**

1. 字体会自动安装到 `C:\Windows\Fonts\` 目录
2. 无需刷新缓存，系统会自动识别

#### macOS 系统

macOS 默认已安装苹方字体（PingFang），插件可直接使用。如需安装其他中文字体：

**方法 1：通过字体册安装**

1. 下载字体文件（`.ttf` 或 `.otf`）
2. 双击字体文件，会打开"字体册"应用
3. 点击"安装字体"按钮

**方法 2：手动复制到字体目录**

```bash
# 复制到用户字体目录
cp your-font.ttf ~/Library/Fonts/

# 或复制到系统字体目录（需要管理员权限）
sudo cp your-font.ttf /Library/Fonts/
```

**推荐字体：**

- **思源黑体**：https://github.com/adobe-fonts/source-han-sans/releases
- **文泉驿微米黑**：https://sourceforge.net/projects/wqy/files/wqy-microhei/

#### Linux 安装后刷新字体缓存

```bash
fc-cache -fv
```

#### 支持的字体路径

插件会按优先级自动查找以下字体（只需安装其中一个即可）：

1. `/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc` ⭐ **文泉驿正黑**（推荐，作者使用）
2. `/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf` (Droid)
3. `/usr/share/fonts/truetype/wqy/wqy-microhei.ttc` (文泉驿微米黑)
4. `/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc` (Noto CJK)
5. `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` (Noto CJK)
6. `/System/Library/Fonts/PingFang.ttc` (macOS 苹方)
7. `C:/Windows/Fonts/msyh.ttc` (Windows 微软雅黑)

### 安装 Python 依赖

```bash
# 安装必需依赖
pip install Pillow

# 安装可选依赖（建议安装以支持时区功能）
pip install pytz
```

或使用 requirements.txt：

```bash
pip install -r requirements.txt
```

## 安装和配置

1. 将插件放置到 `plugins/chat_summary_plugin/` 目录
2. 安装 Python 依赖（Pillow 和 pytz）
3. 确保系统已安装中文字体（见上方"系统字体"部分）
4. 编辑 `config.toml` 文件，将 `plugin.enabled` 设置为 `true`
5. 根据需要调整其他配置项（见下方配置选项）
6. 重启 MaiBot

## 使用方法

### 基本命令

```
#summary                        # 今天的群聊总结
#summary 2025-01-12             # 指定日期的群聊总结
#summary 123456789 2025-01-12   # 指定QQ用户在该日期的聊天总结
```

### 用户识别方式

仅支持通过 QQ 号识别用户：

1. **直接输入QQ号** - 输入纯数字的QQ号

## 配置选项

编辑 `config.toml`：

```toml
# 插件基本配置
[plugin]
config_version = "1.0.0"     # 配置文件版本
enabled = true               # 是否启用插件

# 总结功能配置
[summary]
group_summary_max_words = 600      # 群聊总结字数限制
user_summary_max_words = 400       # 用户总结字数限制
enable_user_summary = true         # 是否启用用户总结功能
enable_user_titles = true          # 是否启用群友称号分析
enable_golden_quotes = true        # 是否启用金句提取
enable_depression_index = true     # 是否启用炫压抑指数分析

# 每日自动总结配置
[auto_summary]
enabled = false                    # 是否启用每日自动总结
time = "23:00"                     # 自动总结时间（HH:MM格式，24小时制）
timezone = "Asia/Shanghai"         # 时区设置（需安装 pytz）
min_messages = 10                  # 生成总结所需的最少消息数量
target_chats = []                  # 目标群聊QQ号列表（为空则对所有群聊生效）
```

### 自动总结功能说明

启用 `auto_summary.enabled = true` 后：

- **精确调度** - 每天在指定时间自动生成总结，采用精确计算等待时间的方式，不使用轮询
- **时区支持** - 支持自定义时区（需安装 `pytz`），默认为 `Asia/Shanghai`
- **智能过滤** - 只为达到最小消息数（`min_messages`）的群聊生成总结
- **目标群聊** - 可通过 `target_chats` 指定特定群聊，留空则为所有符合条件的群聊生成
- **避免重复** - 每天只执行一次，避免重复生成

配置示例（仅为指定群聊生成）：

```toml
[auto_summary]
enabled = true
time = "23:00"
timezone = "Asia/Shanghai"
min_messages = 10
target_chats = [123456789, 987654321]  # 只为这些群生成总结
```

配置示例（为所有活跃群聊生成）：

```toml
[auto_summary]
enabled = true
time = "23:00"
timezone = "Asia/Shanghai"
min_messages = 10
target_chats = []  # 留空 = 为所有活跃群聊生成
```

## 图片输出特性

总结将以精美图片形式输出，包含以下元素：

- **标题和时间** - 总结标题和日期信息
- **总结内容** - AI生成的个性化总结文本
- **统计信息** - 消息数量、参与人数等
- **群友称号** - 智能分析的群友特色称号（群聊总结）
- **金句语录** - 提取的群聊精彩语录（群聊总结）
- **炫压抑指数** - 群友压抑指数评分和文言文评价（群聊总结）
- **发言分布** - 24小时发言活跃度分布图（群聊总结）
- **装饰元素** - 精美的装饰图案，提升视觉效果

图片生成失败时会自动降级为文本输出，确保功能可用性。

## 技术实现

### 架构设计

- **命令处理** - 使用 `BaseCommand` 处理 `#summary` 命令
- **事件处理** - 使用 `BaseEventHandler` 实现定时任务（`ON_START` 事件）
- **调度器** - `SummaryScheduler` 管理每日自动总结，采用精确等待时间计算
- **数据查询** - 通过 `database_api` 查询聊天记录
- **AI生成** - 使用 `llm_api` 生成智能总结，注入机器人人设确保输出符合角色特点
- **图片生成** - `SummaryImageGenerator` 生成精美总结图片
- **聊天分析** - `ChatAnalysisUtils` 提供用户统计、称号分析、金句提取等功能

### 模块结构

```
chat_summary_plugin/
├── plugin.py                 # 插件主文件
├── config.toml              # 配置文件
├── core/                    # 核心功能模块
│   ├── image_generator.py  # 图片生成器
│   └── chat_analysis.py    # 聊天分析工具
├── decorations/            # 装饰图片资源
└── requirements.txt        # Python依赖
```

## 注意事项

1. **生成时间** - 总结生成需要几秒钟时间（取决于消息数量和AI响应速度），请耐心等待
2. **消息过滤** - 仅统计普通聊天消息，不包括命令消息和系统通知
3. **时间范围** - 仅支持 YYYY-MM-DD 日期格式，省略日期默认今天
4. **自动总结** - 每天只执行一次，避免重复
5. **图片输出** - 图片生成失败会自动降级为文本输出，不影响核心功能
6. **字体要求** - 系统必须安装中文字体，否则图片生成会失败（详见"依赖要求"中的"系统字体"部分）
7. **人设注入** - 总结内容会根据 MaiBot 的人设和回复风格生成，确保符合机器人角色特点

## 权限要求

插件需要以下权限：

- `database.messages.read` - 读取聊天消息记录
- `llm.generate` - 调用 LLM 生成总结
- `send.image` - 发送图片消息
- `send.text` - 发送文本消息

## 开发信息

- **版本**: 1.0.0
- **作者**: 久远 ([saberlights](https://github.com/saberlights))
- **许可证**: GPL-3.0-or-later
- **仓库**: [github.com/saberlights/chat_summary_plugin](https://github.com/saberlights/chat_summary_plugin)
- **类别**: Utilities, Analysis, AI
- **关键词**: chat, summary, analysis, ai, 群聊, 总结, 分析, 图片生成, 定时任务

## 更新日志

### v1.0.0 (2025-11-15)

- ✨ 初始版本发布
- ✨ 支持群聊整体总结
- ✨ 支持单个用户总结
- ✨ 支持指定日期（YYYY-MM-DD）时间范围
- ✨ 支持智能群友称号分析
- ✨ 支持群聊金句提取（群圣经）
- ✨ 支持炫压抑指数分析（AI评估用户发言情绪）
- ✨ 支持精美图片输出（霓虹赛博朋克风格）
- ✨ 支持每日定时自动总结
- ✨ 支持24小时发言活跃度分布图表
- 🎨 精美装饰元素和渐变背景
- 🔧 智能降级机制（图片生成失败自动降级为文本）

## 故障排除

### 图片生成失败

如果图片生成失败，插件会自动降级为文本输出。常见原因：

#### 1. Pillow 未安装

```bash
pip install Pillow
```

#### 2. 缺少中文字体

**检查字体是否安装：**

**Linux 系统：**

```bash
# 检查系统中的中文字体
fc-list :lang=zh

# 验证特定字体路径是否存在
ls -la /usr/share/fonts/truetype/wqy/wqy-zenhei.ttc
```

如果输出为空或文件不存在，说明需要安装中文字体。

**Windows 系统：**

```cmd
# 检查微软雅黑字体是否存在
dir C:\Windows\Fonts\msyh.ttc

# 或者打开字体文件夹查看
explorer C:\Windows\Fonts
```

在打开的文件夹中查找"微软雅黑"或其他中文字体。

**macOS 系统：**

```bash
# 检查苹方字体是否存在
ls -la /System/Library/Fonts/PingFang.ttc

# 或使用字体册查看
open /Applications/Font\ Book.app
```

在字体册中搜索"PingFang"或其他中文字体。

---

**安装字体（Linux）：**

**推荐安装：文泉驿正黑字体**（本插件作者使用，轻量级且兼容性好）

**Debian/Ubuntu 系统：**

```bash
sudo apt-get update
sudo apt-get install fonts-wqy-zenhei
```

**CentOS/RHEL 系统：**

```bash
sudo yum install wqy-zenhei-fonts
```

**Arch Linux：**

```bash
sudo pacman -S wqy-zenhei
```

**安装后刷新字体缓存（仅 Linux）：**

```bash
fc-cache -fv
```

**Windows 和 macOS 字体安装：**

详见上方"依赖要求 → 系统字体"部分。

#### 3. 装饰图片缺失

检查 `decorations/` 目录是否完整：

```bash
ls -la plugins/chat_summary_plugin/decorations/
```

应该包含以下文件：
- `decoration1.png`
- `decoration2.png`
- `decoration3.png`
- `decoration4.png`
- `decoration5.png`
- `decoration_star.png`
- `decoration_sparkle.png`
- `decoration_heart.png`
- `decoration_bubble.png`
- `decoration_quote.png`
- `decoration_corner.png`

### 自动总结不执行

检查以下配置：

- `plugin.enabled` 和 `auto_summary.enabled` 都需要为 `true`
- 时间格式正确：`HH:MM`（24小时制）
- 如果使用时区功能，确保已安装 `pytz`
- 群聊消息数量是否达到 `min_messages` 要求

### 用户识别失败

- 确保使用正确的识别方式（@提及、@昵称、QQ号）
- QQ号必须是纯数字
- 昵称/群名片需要在聊天记录中存在

## 贡献

欢迎提交 Issue 和 Pull Request！

## 许可证

本项目采用 GPL-3.0-or-later 许可证。详见 LICENSE 文件。
