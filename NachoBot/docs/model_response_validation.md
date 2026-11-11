# 模型响应验收

`LLMRequest.generate_response_async` 和 `llm_api.generate_with_model` 支持可选的仅限关键字参数
`response_validator: Callable[[str], None]`。同步校验器返回 `None` 表示接受；拒绝当前候选时抛出
`ModelOutputValidationError`。异常使用固定消息，不携带模型文本。非可调用对象、异步校验器、校验器
编程错误及非 `None` 返回值都会结束本次调用，不会惩罚或切换候选模型。

校验在候选执行器或 provider 返回之后、缓存诊断之后运行，每个选中的模型最多调用一次。校验器收到
与公开响应相同的文本：`content=None` 转成空字符串；provider 未提供 reasoning 时，沿用现有
`<think>` 提取；非字符串响应内容会被拒绝。拒绝复用现有候选评分与回退，不增加格式重试，也不改变
候选选择顺序。成功响应的文本、工具调用、reasoning 和元数据仍按现有路径处理。

能力路由、联网搜索判定和预约解析都使用现有有界 JSON 对象解析器，并各自校验消费所需字段。能力路由
仍保留目录过滤、工具数量限制、权限闸口和确定性回退；联网搜索的字符串布尔值按原消费者逻辑处理，
不去除首尾空格。预约解析继续沿用置信度限幅、阈值、日期解析、追问和正则回退。正则回退仍可能在识别到
有效提醒时间时返回 `is_reminder=True`、置信度 `0.75`；这项校验不保证产出否定提醒结果。

拒绝响应在校验边界记录一次 provider usage；校验程序错误或校验期间取消时，只要响应已返回也会记录。
成功响应仍由公开生成边界记录一次。记录器失败不会覆盖校验或取消错误，也不会重放 provider 请求。数据库
现有 `success` 状态表示 provider 推理已完成，不表示业务验收通过；数据库记录失败时不保证 usage 持久化。

## 图像感知响应 schema

`POST /api/multimodal/perception` 可为 `image.describe.v1` 接收可选的顶层
`response_schema` 对象。省略或传入 `null` 时保持原有自由文本行为；音频、视频等其他操作携带 schema
会以 HTTP 400 拒绝。schema 在选择候选前校验、复制并固定，限制为 16 KiB、JSON 结构深度 8 和 128
个节点；根节点必须声明 `type: "object"`。每个 schema 节点必须声明单一 `type`，仅支持
`object`、`string`、`number`、`integer`、`boolean`、`null`，以及适用于对象、字符串或数字类型的
`properties`、`required`、布尔值 `additionalProperties`、`minimum`、`maximum`、`minLength` 和
`maxLength`。数组、引用（包括 `$ref`）、正则表达式、格式、未知关键字、远程解析及类型强制均不支持。

每个候选交给验收器的响应文本最多 10,000 个字符，并必须是严格 JSON：重复对象键、非有限数字、代码围栏、附加尾随文本、
超过 32 层或 4,096 个节点的 JSON 均被拒绝。首尾 JSON 空白是合法语法；除此以外不做围栏或 prose 修复。
schema 不匹配会通过现有调度器惩罚该候选并按既有选模算法继续尝试；不会增加格式重试或调整候选顺序。
所有候选不匹配时仍沿用现有感知降级响应。该契约只校验调用者声明的通用 JSON 形状，不携带平台专属字段或
应用策略。

可移植验证命令：

```powershell
py -3.11 -B -m unittest discover -s tests -p test_model_response_validation.py
py -3.12 -B -m unittest discover -s tests -p test_model_response_validation.py
```
