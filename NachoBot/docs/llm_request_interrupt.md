# LLM 请求中断边界

`LLMRequest` 在模型选择、候选执行、SDK 请求和重试之间共享同一个
`asyncio.Event`。当调用方设置该事件时，调度器会在下一处检查点抛出
`ReqAbortException`，避免开始下一次请求、切换到另一个模型，或把已经过时的
响应交给调用方。

检查点覆盖：

- 模型选择之前，以及模型选择后仍处于使用量租约的 `try/finally` 内；
- 候选执行器调用默认请求闭包之前，避免继续构建消息；
- 每次 SDK 请求之前和响应返回之后；
- 失败分类、重试计数、模型惩罚和故障切换之前；
- 固定重试等待期间。事件会唤醒等待；计时器和事件同时就绪时，已设置的事件
  优先中断。

当中断发生在模型选择之后，现有 `finally` 仍会释放该模型的一次使用量租约。
预先设置事件时，调度器会在选择模型和构造客户端之前退出。模型候选策略、正常
重试次数、重试间隔、HTTP 400 兼容性回退、413 消息压缩及缓存诊断元数据语义
保持原样。

## Event 与 Task.cancel()

显式设置 `interrupt_flag` 会通过 `ReqAbortException` 结束请求。调用方直接对
当前 `asyncio.Task` 调用 `cancel()` 时，仍会收到 `asyncio.CancelledError`；调度器
不会把任务取消翻译成请求中断。重试等待不会遗留事件等待者。

## 边界

这些检查不会强制停止已经运行的远端推理，也不会停止
`asyncio.to_thread` 中正在进行的同步消息压缩。正在执行的 SDK 请求如何响应事件，
取决于该客户端自身的取消处理。即使 SDK 或候选执行器在中断后才返回，调度器也会
在发布成功结果或记录成功诊断之前检查事件并丢弃该结果。

可用以下隔离测试检查共享调度器边界，不会导入真实配置、日志数据库或应用包：

```powershell
NachoBot/.venv/Scripts/python.exe -B -m unittest discover -s NachoBot/tests -p test_llm_request_interrupt.py -v
```
