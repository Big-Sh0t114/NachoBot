# Changelog

本文件记录 `MaiBot_MCPBridgePlugin` 的用户可感知变更。

## 2.0.0

- 配置入口统一：MCP 服务器仅使用 Claude Desktop `mcpServers` JSON（`servers.claude_config_json`）
- 兼容迁移：自动识别旧版 `servers.list` 并迁移为 `mcpServers`（需在 WebUI 保存一次固化）
- 保持功能不变：保留 Workflow（硬流程/工具链）与 ReAct（软流程）双轨制能力
- 精简实现：移除旧的 WebUI 导入导出/快速添加服务器实现与 `tomlkit` 依赖
- 易用性：完善 Workflow 变量替换（支持数组下标与 bracket 写法），并优化 WebUI 配置区顺序

## 1.9.0

- 双轨制架构：ReAct（软流程）+ Workflow（硬流程/工具链）

## 1.8.0

- Workflow（工具链）：多工具顺序执行、变量替换、自定义 Workflow 并注册为组合工具

## 1.7.0

- 断路器模式、状态刷新、工具搜索等易用性增强

