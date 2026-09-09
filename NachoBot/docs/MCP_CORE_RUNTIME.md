# MCP 核心运行时

NachoBot 由 `src/mcp` 统一持有 MCP 服务器连接、工具目录、权限检查和调用结果归一化。普通工具仍使用原有 `ToolExecutor`；只有能力路由器判定当前请求确实需要 MCP 时，才启动独立且有轮数、调用数上限的 MCP 工具链。

## 核心配置

服务器只从独立的 `config/mcp_config.toml` 的 `[mcp].servers_json` 加载。该字段使用 Claude Desktop 风格的 `mcpServers` JSON：

```toml
[mcp]
enabled = true
servers_json = '''
{
  "mcpServers": {
    "example": {
      "command": "example-mcp-server",
      "args": ["--stdio"]
    }
  }
}
'''
```

部署配置不会被 Git 跟踪。令牌、鉴权头和私有用户 ID 只能保存在部署配置或受保护的环境变量中，不要写入模板、文档或日志。

## 当前推荐服务器组合

- `filesystem-local`：受目录参数约束的本地文件读取与搜索。生产环境应只传入真正需要的根目录，并通过 `disabled_tools` 禁用写入类工具。
- `playwright-local`：使用 Microsoft 维护的 `@playwright/mcp` 替代已归档的 Puppeteer MCP。默认采用 headless、isolated，省略图片响应，并把运行产物写到未跟踪的部署配置目录；高风险的任意代码和文件上传工具应禁用。
- `context7-docs`：按库和版本检索当前技术文档。无 API Key 时可使用基础额度；需要更高限额时再在未跟踪配置中加入凭据。

普通公共网页查询继续使用现有联网搜索链，不重复注册 Fetch MCP。当前 Python Fetch 参考服务器尚未完成 SDK 2.x 迁移，强行锁定旧 SDK 还会产生协议发现警告。不要注册 `everything` 一类协议演示服务器。Git、GitHub、数据库和云平台服务器只有在明确配置最小权限凭据、确认工具写入边界后才应启用。

## 权限与执行边界

- 工具目录在交给能力路由模型前按当前用户和群过滤。
- 实际调用前再次按相同上下文鉴权，避免目录判定与执行之间绕过权限。
- 核心配置默认启用权限并采用 `deny_all`，部署者需要显式配置允许用户或规则。
- MCP 名称、描述、参数结构和返回结果均按不可信数据处理；媒体内容不会把 base64 原文注入模型上下文。
- 工具调用异常不会自动重试，避免响应丢失时重复执行发送、删除或写入操作。
- 断线重连采用最长 15 分钟的指数退避。
- 能力路由目录会压缩描述，但保留所有已注册工具的名称；执行阶段最多暴露配置数量的候选工具。

## 服务器兼容性

核心客户端使用项目锁定的 `mcp>=2.0.0` API。不能为迁就旧服务器而降级 NachoBot 核心 SDK。Windows 上的 Node MCP 服务器推荐通过 `cmd /c npx ...` 启动，避免 `.cmd` 解析差异。

旧 `MaiBot_MCPBridgePlugin` 已退出运行路径；核心不再读取其配置，也不依赖其启动、停止事件或 WebUI 状态面板。
