# MCP 桥接插件开发文档

本文档面向开发者，介绍插件的架构设计、核心模块和扩展方式。

## 架构概览

```
MaiBot_MCPBridgePlugin/
├── plugin.py          # 主插件文件，包含所有核心逻辑
├── mcp_client.py      # MCP 客户端封装
├── tool_chain.py      # 工具链（Workflow）模块
├── core/
│   └── claude_config.py # Claude Desktop mcpServers 解析/迁移
├── config.toml        # 运行时配置
└── _manifest.json     # 插件元数据
```

## 核心模块

### 1. MCP 客户端 (`mcp_client.py`)

封装了与 MCP 服务器的通信逻辑。

```python
from .mcp_client import mcp_manager, MCPServerConfig, TransportType

# 添加服务器
config = MCPServerConfig(
    name="my-server",
    transport=TransportType.STREAMABLE_HTTP,
    url="https://mcp.example.com/mcp"
)
await mcp_manager.add_server(config)

# 调用工具
result = await mcp_manager.call_tool("server_tool_name", {"param": "value"})
if result.success:
    print(result.content)
```

**支持的传输类型：**
- `STDIO`: 本地进程通信
- `SSE`: Server-Sent Events
- `HTTP`: HTTP 请求
- `STREAMABLE_HTTP`: 流式 HTTP（推荐）

### 2. 工具注册系统

MCP 工具通过动态类创建注册到 MaiBot：

```python
# 创建工具代理类
class MCPToolProxy(BaseTool):
    name = "mcp_server_tool"
    description = "工具描述"
    parameters = [("param", ToolParamType.STRING, "参数描述", True, None)]
    available_for_llm = True
    
    async def execute(self, function_args):
        result = await mcp_manager.call_tool(self._mcp_tool_key, function_args)
        return {"name": self.name, "content": result.content}
```

### 3. 工具链模块 (`tool_chain.py`)

实现 Workflow 硬流程，支持多工具顺序执行。

```python
from .tool_chain import ToolChainDefinition, ToolChainStep, tool_chain_manager

# 定义工具链
chain = ToolChainDefinition(
    name="search_and_detail",
    description="搜索并获取详情",
    input_params={"query": "搜索关键词"},
    steps=[
        ToolChainStep(
            tool_name="mcp_server_search",
            args_template={"keyword": "${input.query}"},
            output_key="search_result"
        ),
        ToolChainStep(
            tool_name="mcp_server_detail",
            args_template={"id": "${prev}"}
        )
    ]
)

# 注册并执行
tool_chain_manager.add_chain(chain)
result = await tool_chain_manager.execute_chain("search_and_detail", {"query": "test"})
```

**变量替换语法：**
- `${input.参数名}`: 用户输入
- `${step.输出键}`: 指定步骤的输出
- `${prev}`: 上一步输出
- `${prev.字段}`: 上一步输出（JSON）的字段
- `${step.geo.return.0.location}` / `${step.geo.return[0].location}`: 数组下标访问
- `${step.geo['return'][0]['location']}`: bracket 写法（最通用）

## 双轨制架构

### ReAct 软流程

将 MCP 工具注册到 MaiBot 的记忆检索 ReAct 系统，LLM 自主决策调用。

```python
def _register_tools_to_react(self) -> int:
    from src.memory_system.retrieval_tools import register_memory_retrieval_tool
    
    def make_execute_func(tool_key: str):
        async def execute_func(**kwargs) -> str:
            result = await mcp_manager.call_tool(tool_key, kwargs)
            return result.content if result.success else f"失败: {result.error}"
        return execute_func
    
    register_memory_retrieval_tool(
        name="mcp_tool_name",
        description="工具描述",
        parameters=[{"name": "param", "type": "string", "required": True}],
        execute_func=make_execute_func("tool_key")
    )
```

### Workflow 硬流程

用户预定义的固定执行流程，注册为组合工具。

```python
def _register_tool_chains(self) -> None:
    from src.plugin_system.core.component_registry import component_registry
    
    for chain_name, chain in tool_chain_manager.get_enabled_chains().items():
        info, tool_class = tool_chain_registry.register_chain(chain)
        info.plugin_name = self.plugin_name
        component_registry.register_component(info, tool_class)
```

## 配置系统

### MCP 服务器配置（Claude Desktop 规范）

插件只接受 Claude Desktop 的 `mcpServers` JSON（见 `core/claude_config.py`）。配置入口统一为：

- WebUI/配置文件：`[servers].claude_config_json`
- 命令：`/mcp import`（合并 `mcpServers`）与 `/mcp export`（导出当前 `mcpServers`）

兼容迁移：
- 若检测到旧版 `servers.list`，会自动迁移为 `servers.claude_config_json`（仅迁移到内存配置，需 WebUI 保存一次固化）。

### WebUI 配置 Schema

使用 `ConfigField` 定义 WebUI 配置项：

```python
config_schema = {
    "section_name": {
        "field_name": ConfigField(
            type=str,                    # 类型: str, bool, int, float
            default="default_value",     # 默认值
            description="字段描述",
            label="显示标签",
            input_type="textarea",       # 输入类型: text, textarea, password
            rows=5,                      # textarea 行数
            disabled=True,               # 只读
            choices=["a", "b"],          # 下拉选项
            hint="提示信息",
            order=1,                     # 排序
        ),
    },
}
```

### 配置读取

```python
# 在组件中读取配置
value = self.get_config("section.key", default="fallback")

# 在插件类中读取
value = self.config.get("section", {}).get("key", "default")
```

## 事件处理

### 启动事件

```python
class MCPStartupHandler(BaseEventHandler):
    event_type = EventType.ON_START
    handler_name = "mcp_startup"
    
    async def execute(self, message):
        global _plugin_instance
        if _plugin_instance:
            await _plugin_instance._async_connect_servers()
        return (True, True, None, None, None)
```

### 停止事件

```python
class MCPStopHandler(BaseEventHandler):
    event_type = EventType.ON_STOP
    handler_name = "mcp_stop"
    
    async def execute(self, message):
        await mcp_manager.shutdown()
        return (True, True, None, None, None)
```

## 命令系统

```python
class MCPStatusCommand(BaseCommand):
    command_name = "mcp_status"
    command_pattern = r"^/mcp(?:\s+(?P<action>\S+))?(?:\s+(?P<arg>.+))?$"
    
    async def execute(self) -> Tuple[bool, str, bool]:
        action = self.matched_groups.get("action", "")
        arg = self.matched_groups.get("arg", "")
        
        if action == "tools":
            await self.send_text("工具列表...")
        elif action == "reconnect":
            await self._handle_reconnect(arg)
        
        return (True, None, True)  # (成功, 消息, 拦截)
```

## 高级功能

### 调用追踪

```python
from plugin import tool_call_tracer, ToolCallRecord

# 记录调用
record = ToolCallRecord(
    call_id="xxx",
    timestamp=time.time(),
    tool_name="tool",
    server_name="server",
    arguments={"key": "value"},
    success=True,
    duration_ms=100.0
)
tool_call_tracer.record(record)

# 查询记录
recent = tool_call_tracer.get_recent(10)
by_tool = tool_call_tracer.get_by_tool("tool_name")
```

### 调用缓存

```python
from plugin import tool_call_cache

# 配置缓存
tool_call_cache.configure(
    enabled=True,
    ttl=300,           # 秒
    max_entries=200,
    exclude_tools="mcp_*_time_*"  # 排除模式
)

# 使用缓存
cached = tool_call_cache.get("tool_name", {"param": "value"})
if cached is None:
    result = await call_tool(...)
    tool_call_cache.set("tool_name", {"param": "value"}, result)
```

### 权限控制

```python
from plugin import permission_checker

# 配置权限
permission_checker.configure(
    enabled=True,
    default_mode="allow_all",  # 或 "deny_all"
    rules_json='[{"tool": "mcp_*_delete_*", "denied": ["qq:123:group"]}]',
    quick_deny_groups="123456789",
    quick_allow_users="111111111"
)

# 检查权限
allowed = permission_checker.check(
    tool_name="mcp_server_delete",
    chat_id="123456",
    user_id="789",
    is_group=True
)
```

### 断路器模式

MCP 客户端内置断路器，故障服务器快速失败：

- 连续失败 N 次后熔断
- 熔断期间直接返回错误
- 定期尝试恢复

## 扩展开发

### 添加新的传输类型

1. 在 `mcp_client.py` 中添加 `TransportType` 枚举值
2. 实现对应的连接逻辑
3. 更新 `_create_transport()` 方法

### 添加新的工具类型

1. 继承 `BaseTool` 创建新类
2. 在 `get_plugin_components()` 中注册
3. 实现 `execute()` 方法

### 添加新的命令

1. 在 `MCPStatusCommand.execute()` 中添加新的 action 分支
2. 或创建新的 `BaseCommand` 子类

## 调试技巧

### 日志级别

```python
from src.common.logger import get_logger
logger = get_logger("mcp_bridge_plugin")

logger.debug("详细调试信息")
logger.info("一般信息")
logger.warning("警告")
logger.error("错误")
```

### 常用调试命令

```bash
/mcp                    # 查看状态
/mcp tools              # 查看工具列表
/mcp trace              # 查看调用记录
/mcp cache              # 查看缓存状态
/mcp chain              # 查看工具链
```

## 更新日志

见 `plugins/MaiBot_MCPBridgePlugin/CHANGELOG.md`

## 开发约定

- 本仓库不提交测试脚本/临时复现文件；如需本地验证，可自行在工作区创建未跟踪文件（建议放到 `.local/` 并加入 `.gitignore`）。
