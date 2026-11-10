# MCP 连接操作时限

`connect_timeout` 为一次连接准备提供单一预算，覆盖等待该服务器的连接锁、异步客户端工厂、进入客户端上下文和工具发现。已连接时仍直接返回成功。`call_timeout` 覆盖等待同一服务器的连接锁和一次 SDK 工具调用。

调用在取得锁并派发给 SDK 前超时或被取消时，不会调用 SDK，也不会断开健康连接或清空工具目录。派发后的超时沿用现有失效处理：关闭连接、清空该服务器工具，并且不重试。远端写入可能已经生效，因此超时结果不代表操作未执行。调用方主动取消仍传播 `CancelledError`。

设置阶段已有候选客户端后若失败或取消，会尽力关闭候选对象一次；取消会继续向上传播。配置的操作时限不包含已有的最多 10 秒关闭宽限期。时限依赖 asyncio/SDK 合作响应取消；它不能强制停止忽略取消的原生或远端操作，但晚到的成功结果会被丢弃，不能发布为成功或新连接。

所有 MCP 工具类型和聊天入口共用 `src/mcp/service.py` 中的 `_ServerConnection`。需要复用此行为时，应继续在该连接类维护锁与操作边界，无需在具体工具适配层另加时限。

本地验证命令（在 `NachoBot` 目录运行）：

```powershell
.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_mcp_connection_deadlines.py -v
.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_mcp_tool_executor.py -v
.venv/Scripts/python.exe -m ruff check src/mcp/service.py tests/test_mcp_connection_deadlines.py
```
