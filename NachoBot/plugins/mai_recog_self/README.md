# 自身角色识别插件

这是 NachoBot 原生插件，使用 `plugins.mai_recog_self.backend` 后端对收到的 JPEG、PNG
和 WebP 图片及表情做本地角色识别。多角色模型按其冻结类别 ID 返回 `nachobot`、
`shizukubot`、`moli` 或 `other`；只有后端接受的具体角色会在对应图片后追加临时提示。
`other`、不确定、模型未准备好、超时或失败时，消息内容保持原样。插件不会把聊天图片加入训练集。

`recognize_self_image` 和 `recognize_character_image` 各自只接受当前工具调用上下文列出的真实
`message_id` 和该消息内从 0 开始的图片序号。工具不能搜索全局图片、猜测 Planner 别名，
或读取其他聊天的媒体。前者保留自身角色判断语义；后者把自然语言识别摘要交给 Replyer。插件直接返回值中的
`data` 保留完整结构化结果，宿主 `ToolExecutor` 只会把 `content` 注入对话。

### 宿主兼容性

这些工具要求宿主提供 `src.plugin_system.base.tool_execution_context.ToolExecutionPreparation`，并且
`ToolExecutor` 支持工具同时实现 `prepare_execution_context` 与 `execute_with_context` 的调用路径。
manifest 中的 `min_version = 0.9.2` 只是版本下限，不保证这些能力存在。若宿主缺少上下文模块或类型，
插件加载会明确报错并提示更新；配套的 `ToolExecutor` 调用路径也必须由宿主提供。插件不会以缺少调用上下文的方式继续媒体访问。

配置保存在 `config.toml`：

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `plugin.enabled` | `true` | 注册识别事件和工具 |
| `plugin.version` | `1.0.0` | 插件配置中的版本标记 |
| `recognition.character_name` | 空字符串 | 留空时使用当前 Bot 昵称 |
| `recognition.self_character_id` | 空字符串 | 多角色模型中当前 Bot 自身的稳定类别 ID；留空时 `is_self` 为未知，不从昵称推断 |
| `recognition.worker_python` | 空字符串 | 显式指定 worker Python；留空时依次选择可用的同级 `NachoBot-Multimodal-Adapter/.venv` 和当前 Core Python。目录库中保存的解释器路径不会自动采用 |
| `recognition.data_dir` | 空字符串 | 留空时使用 `Core/data/plugins/mai_recog_self`；相对路径以 Core 根目录为基准 |
| `recognition.auto_recognize` | `true` | 是否在消息预处理中追加阳性提示 |
| `recognition.budget_seconds` | `2.5` | 单条消息总预算，最高 6 秒 |

训练完成后，可从 Core 目录运行本插件内的 `plugins/mai_recog_self/setup_self_recognition.ps1`，用明确的模型版本和自身类别ID一次完成CPU校验、模型启用、真实推理探针和配置更新。省略探针路径时会使用冻结快照中该自身类别的TRAIN图片。脚本只会将缺失的小依赖放入该数据目录的 `worker_deps`，不会改动共享worker环境；worker自动选择会先在隔离子进程中检查同级 Multimodal 环境是否能导入Torch和torchvision，再回退到Core当前Python。若Core没有所需框架，部署会报告依赖缺失；也可用配置或 `--worker-python` 显式指定解释器。也可直接运行 `python plugins/mai_recog_self/deploy.py --help` 查看CLI参数。详细部署和回滚行为见 [self-recognition 后端说明](BACKEND.md#worker-python选择)。

训练、模型版本和依赖环境由插件内后端 CLI 管理。v2 多角色模型使用后端提供的 `recognized`
和 `character_id` 决策，不由插件重新应用旧二分类分数阈值；`is_self` 单独比较
`recognition.self_character_id`。旧二分类模型继续使用原有 `character_name` 和分数判断，
且在原不确定区间内保持未知。正常使用无需在插件目录安装 PyTorch；模型推理留在后端
worker 进程中。卸载插件时会关闭该插件创建的 worker。

### 日志

预处理会为实际处理的每张图片或表情写一条有界 JSON 事件到主日志，记录消息 ID、图片序号、识别状态和原因、
预测/接受的类别、原始 softmax 分数与多类别 margin、期望/实际模型版本，以及提示是否加入。缓存命中也会记录。
图片内容、媒体 URL、聊天文本和完整分类字典不会写入这些事件；分数是原始模型输出，不是校准百分比。文本消息不会
产生逐图日志。达到处理预算或数量上限时只记录一次停止原因；worker 启动、加载和推理故障也会记录到主日志。
后台 worker 的 stderr 仍追加到数据目录的 `inference.log`，同时插件只在内存中保留当前 worker 最近 8 KiB，
以便在主日志中报告它的终止异常原因；历史 stderr 不会被当作新进程的错误。

This adaptation is licensed under GPL-3.0-or-later. See [NOTICE.md](NOTICE.md)
for attribution and scope.
