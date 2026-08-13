# A_Memorix

NachoBot 内置的长期记忆与认知增强子系统（v2.0.0）。它统一管理文本记忆、关系图谱、Episode、人物画像、导入与检索调优；当前主入口为宿主 host service 与 `core/runtime/sdk_memory_kernel.py`，不再提供旧版独立 `server.py` 或 slash 命令。

## 文档导航

- [快速入门](QUICK_START.md)
- [配置参考](CONFIG_REFERENCE.md)
- [导入指南](IMPORT_GUIDE.md)
- [修改约定](MODIFICATION_POLICY.md)
- [更新日志](CHANGELOG.md)

## 核心能力

- 向量 + 图谱双路召回，支持 `search`、`time`、`hybrid`、`episode`、`aggregate`。
- `external_id` 幂等写入、段落/关系去重与 Episode pending 队列。
- 人物画像自动快照与手动 override。
- 来源、图谱、Episode、画像、导入、调优、删除/恢复等管理接口。
- 检索由 SDK kernel 单次编排，避免宿主与子检索链路重复回流。

## 常用 Tool

| Tool | 用途 |
| --- | --- |
| `search_memory` | 检索长期记忆 |
| `ingest_summary` / `ingest_text` | 写入摘要或普通文本 |
| `get_person_profile` | 获取人物画像 |
| `maintain_memory` | 强化、保护、恢复、冻结或回收记忆 |
| `memory_stats` | 查看统计 |
| `memory_graph_admin` | 图谱节点与边管理 |
| `memory_source_admin` | 来源查询与删除 |
| `memory_episode_admin` | Episode 查询、重建与 pending 处理 |
| `memory_profile_admin` | 画像查询与 override |
| `memory_import_admin` / `memory_tuning_admin` | 导入与检索调优任务 |
| `memory_delete_admin` | 删除预览、执行、恢复与清理 |

`search_memory.mode` 只接受 `search/time/hybrid/episode/aggregate`；`time` 和 `hybrid` 必须提供 `time_start` 或 `time_end`。旧 `semantic` 模式已移除。

## 快速开始

在 `NachoBot` 目录同步完整项目依赖：

```bash
uv sync --locked
```

首次启用后，配置位于 `config/a_memorix.toml`。建议先从 WebUI 的长期记忆页面修改常用项，高级项再使用原始 TOML；完整语义见 [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md)。

运行时自检：

```bash
uv run python src/A_memorix/scripts/runtime_self_check.py --json
```

导入文本后，可用 `memory_stats` 和 `search_memory` 验证：

```bash
uv run python src/A_memorix/scripts/process_knowledge.py
```

## 调用示例

```json
{
  "tool": "search_memory",
  "arguments": {
    "query": "项目复盘",
    "mode": "aggregate",
    "limit": 5,
    "chat_id": "group:dev"
  }
}
```

```json
{
  "tool": "ingest_text",
  "arguments": {
    "external_id": "note:2026-08-11:001",
    "source_type": "note",
    "text": "完成 README 维护",
    "chat_id": "group:dev"
  }
}
```

## 配置重点

- `storage.data_dir`：数据库与索引位置。
- `embedding.dimension`：公开的向量维度控制项。
- `embedding.quantization_type`：当前支持 `int8`。
- `retrieval.*` / `retrieval.sparse.*`：召回与稀疏检索。
- `episode.*`、`person_profile.*`、`memory.*`：Episode、画像与记忆策略。
- `web.import.*` / `web.tuning.*`：导入和调优任务。

## 常用脚本

| 脚本 | 用途 |
| --- | --- |
| `process_knowledge.py` | 批量导入文本 |
| `import_lpmm_json.py` / `convert_lpmm.py` | LPMM/OpenIE 导入转换 |
| `migrate_chat_history.py` / `migrate_maibot_memory.py` | 历史数据迁移 |
| `backfill_temporal_metadata.py` | 回填时间元数据 |
| `audit_vector_consistency.py` | 审计向量一致性 |
| `rebuild_episodes.py` | 按来源重建 Episode |
| `runtime_self_check.py` | 验证 embedding 与存储运行时 |

## 常见问题

SQLite 没有 FTS5 时可关闭稀疏检索：

```toml
[retrieval.sparse]
enabled = false
```

出现向量维度不一致时，先运行 `runtime_self_check.py`，确认当前 embedding 输出与已有索引；不要在未备份数据前直接重建存储。

默认许可证为 AGPL-3.0；针对 MaiBot 的额外授权见 [LICENSE-MAIBOT-GPL.md](LICENSE-MAIBOT-GPL.md)。
