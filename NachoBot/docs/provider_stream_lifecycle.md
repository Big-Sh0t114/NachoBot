# Provider 流式响应生命周期

OpenAI 和 Gemini 的流式响应由
`src/llm_models/model_client/stream_lifecycle.py` 统一持有。Provider 把 SDK 请求工厂、现有流处理器和原始中断事件交给
`run_provider_stream`。工厂在 runner 内部调用，处理器收到原始 SDK stream 和同一个中断事件，返回值仍沿用原来的
`(APIResponse, usage_record)` 二元组。

Runner 同时观察 SDK 获取和处理器执行。中断事件会在处理器等待下一帧时生效；普通 `Task.cancel()` 则保留
`CancelledError`。一旦结束或中断，owner 会先同步标记退休，再安排有界的 SDK 请求/处理器排空和公开 stream 关闭。
每个已获取的 stream 只由 owner 发起一次公开关闭尝试，优先调用 `aclose()`，没有时调用 `close()`。清理错误不替代原始
异常、中断或成功结果。

请求或处理器若在 250 毫秒排空窗口后仍未结束，会继续由 detached-task 跟踪器持有。迟到的 SDK 响应会被关闭；若处理器
仍在 `__anext__` 中，owner 会等处理器实际结束后再调用 `aclose()`，避免异步生成器处于运行状态时关闭。关闭操作本身也有
独立的 250 毫秒观察窗口；窗口结束时不会取消关闭任务，关闭会继续运行并保持跟踪。各清理阶段分别计时，这不是整个请求的
总时限。

`utils_model` 中已有的增量观察器仍负责回调和转发。Identity-scoped `ContextVar` 只在当前 owner 的原始 stream
处理期间标记所有权；这样观察器会跳过自己原有的中断关闭回退，避免与 owner 重复关闭。没有使用共享 runner 的自定义
`BaseClient` 仍保留该回退。处理器可以直接关闭原始 SDK 对象；owner 不会拦截这类调用，SDK 关闭方法应按其自身契约处理
重复关闭。

关闭边界只使用 SDK 的公开 `aclose()` 或 `close()`。Gemini 的公开异步生成器内部有多层异步迭代器且没有显式关闭内层
迭代器；调用它的公开 `aclose()` 是 runner 可以保证执行的所有权动作，但不能据此保证底层 HTTP transport 已立即释放。
实现不访问 SDK 私有生成器帧、私有 HTTP 对象或 provider transport hook，也不关闭整个 SDK client。非流式请求路径保持原样。

在 `NachoBot` 目录运行标准库隔离回归：

```powershell
.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_provider_stream_lifecycle.py -v
```
