# Changelog

## [1.0.3] - 2025-3-31
### Added
- 新增了心流相关配置项：
  - `heartflow` 配置项，用于控制心流功能

### Removed
- 移除了 `response` 配置项中的 `model_r1_probability` 和 `model_v3_probability` 选项
- 移除了次级推理模型相关配置

## [1.0.1] - 2025-3-30
### Added
- 增加了流式输出控制项 `stream`
- 修复 `LLM_Request` 不会自动为 `payload` 增加流式输出标志的问题

## [1.0.0] - 2025-3-30
### Added
- 修复了错误的版本命名
- 杀掉了所有无关文件

## [0.0.11] - 2025-3-12
### Added
- 新增了 `schedule` 配置项，用于配置日程表生成功能
- 新增了 `response_splitter` 配置项，用于控制回复分割
- 新增了 `experimental` 配置项，用于实验性功能开关
- 新增了 `llm_observation` 和 `llm_sub_heartflow` 模型配置
- 新增了 `llm_heartflow` 模型配置
- 在 `personality` 配置项中新增了 `prompt_schedule_gen` 参数

### Changed
- 优化了模型配置的组织结构
- 调整了部分配置项的默认值
- 调整了配置项的顺序，将 `groups` 配置项移到了更靠前的位置
- 在 `message` 配置项中：
  - 新增了 `model_max_output_length` 参数
- 在 `willing` 配置项中新增了 `emoji_response_penalty` 参数
- 将 `personality` 配置项中的 `prompt_schedule` 重命名为 `prompt_schedule_gen`

### Removed
- 移除了 `min_text_length` 配置项
- 移除了 `cq_code` 配置项
- 移除了 `others` 配置项（其功能已整合到 `experimental` 中）

## [0.0.5] - 2025-3-11
### Added
- 新增了 `alias_names` 配置项，用于指定麦麦的别名。

## [0.0.4] - 2025-3-9
### Added
- 新增了 `memory_ban_words` 配置项，用于指定不希望记忆的词汇。