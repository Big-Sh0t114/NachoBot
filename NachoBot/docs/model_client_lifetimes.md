# 临时模型客户端的生命周期

`LLMRequest` 会在每个模型候选的选择和执行期间建立一个请求级作用域。选择器把客户端实例和创建时实际使用的 `force_new` 决策交给该作用域：缓存客户端是借用实例，作用域永远不会关闭它；强制新建的客户端由当前候选持有，并在该候选结束后关闭。模型候选的排序、重试和故障切换规则不因清理而改变。

## SDK 工作和关闭时机

OpenAI 和 Gemini 的非流式 `get_response` 会在创建 SDK 请求任务后立即把该任务登记为客户端依赖。即使取消包装请求的协程后 SDK 子任务仍在运行，临时客户端也会保持打开，直到 SDK 任务真正结束。

流式请求在启动操作任务前登记一个完整生命周期 Future。它只有在 SDK 获取与处理操作确实结束，并且已获取的公开流句柄完成一次关闭尝试后才完成；没有获取句柄时，只等待操作任务结束。客户端关闭发生在这两个条件都满足之后。外层最多观察关闭任务 250 毫秒；超时或调用方取消不会取消关闭任务，任务会继续运行并由后台跟踪。清理异常不会替代请求结果、请求错误或中断。

流句柄只通过 SDK 暴露的 `aclose()` 或 `close()` 清理。Gemini 客户端调用公开的 `client.aio.aclose()` 和 `client.close()`；本实现不会访问 SDK 私有传输对象，也不表示这两个公开调用能保证底层 HTTP 连接在某个精确时刻关闭。

## 客户端选择和 ASR 租约

嵌入请求，以及配置了任务超时的请求，会按既有策略创建临时客户端；普通请求继续借用注册表缓存实例。实际 `force_new` 结果在构造后立即捕获，不会在请求结束时根据可能已经变化的配置重新判断所有权。

`acquire_model_lease(eligible_models)` 保留既有默认行为：仍会创建客户端并返回三元组，手动调用方继续负责其原有的客户端生命周期。元数据专用调用可以传 `create_client=False`：它仍按相同候选评分选择模型、应用任务超时元数据并增加使用计数，但不调用客户端工厂，第三个返回值为 `None`。Core ASR 流使用此路径，并由原有租约在流结束时释放使用计数。

## 定向验证

从 `NachoBot` 目录运行。测试只用标准库并从源文件提取实际方法和生命周期辅助模块，不导入应用配置、日志或数据库模块。

```powershell
.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_model_client_lifetimes.py -v
.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_llm_request_interrupt.py -v
.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_provider_stream_lifecycle.py -v
```

也可用 Python 3.11.14 运行相同命令：

```powershell
py -3.11 -B -m unittest discover -s tests -p test_model_client_lifetimes.py -v
py -3.11 -B -m unittest discover -s tests -p test_llm_request_interrupt.py -v
py -3.11 -B -m unittest discover -s tests -p test_provider_stream_lifecycle.py -v
```
