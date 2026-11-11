# ToolExecutor 可见性与执行策略

ToolExecutor 以注册表的 `get_llm_available_tools()` 当前映射作为工具权限来源。聊天禁用项和 include/exclude 前缀会在读取定义、准备上下文、缓存复用以及执行前统一应用。隐藏工具不会读取定义，也不会由 ToolExecutor 构造。注册别名按注册键授权；实例必须与该键当前登记的类完全同型。

一次聊天请求把实际发送给模型的有效工具类绑定到独立请求作用域。准备钩子、提示词格式化和模型请求每次让出控制权后，作用域只会按当前策略收窄；新登记的工具不会加入已经开始的请求。批处理和单工具入口借用同一执行器正在进行的作用域，独立调用则创建自己的快照。请求结束或取消时作用域会失效并清除其强类引用；继承了已结束作用域的子任务不能继续调用旧类。

执行器发现工具已隐藏、被移除或同名类已重载时会拒绝构造或执行。检查发生在构造前、上下文钩子前以及异步执行调用前。已经开始等待工具执行的操作不会被撤销。

结果缓存只复用当前工具目录中名称有效且没有错误项的完整结果组。目录名称、类身份或请求过滤条件改变时，执行器会清理自己的旧结果缓存；结果写入前还会确认请求开始时的完整目录仍与当前目录一致，并拒绝缓存被拒绝、未知、失败或不完整的批次。请求字段使用规范 JSON 数组指纹，避免字段间分隔符造成键冲突。

缓存仍受既有 TTL、用途计数和大小限制约束。工具当前多数为只读操作，但这里没有定义新的缓存资格、幂等性或副作用策略，也不保证相同类后端数据在 TTL 内保持新鲜。MCP 执行器保留自己的缓存关闭和服务端授权路径。

知识库等外部代码可能在调用 ToolExecutor 之前已经构造实例；ToolExecutor 能阻止这类实例执行，但不能撤销调用方先前的构造。

## 隔离验证

以下测试通过 AST 加载实际策略、API getter 和 ToolExecutor 实现，使用标准库假对象，不启动 Core、数据库、模型服务或设备：

```powershell
cd NachoBot
py -3.11 -B -m unittest discover -s tests -p test_tool_execution_policy.py
py -3.11 -B -m unittest discover -s tests -p test_result_cache_lifetime.py
py -3.12 -B -m unittest discover -s tests -p test_tool_execution_policy.py
py -3.12 -B -m unittest discover -s tests -p test_result_cache_lifetime.py
```
