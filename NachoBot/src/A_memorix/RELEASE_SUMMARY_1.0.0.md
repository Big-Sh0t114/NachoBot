# A_Memorix 1.0.0 发布总结

## 范围说明

- 目标版本：`0.7.0` -> `1.0.0`
- 分析基线：`8fe8a0a`（`HEAD -> dev`, `origin/dev`, `origin/v0.7.0-LTSC`, `v0.7.0-LTSC`）
- 本文中的工作树统计，基于 2026-03-06 生成本文与版本元数据修订之前的快照。
- 本任务额外补充的发布元数据修订：`CHANGELOG.md`、`__init__.py`、`plugin.py`、`_manifest.json`、`README.md`、`CONFIG_REFERENCE.md`、`RELEASE_SUMMARY_1.0.0.md`。

## 本次升级主线

### 1. 运行时与插件架构重构

- `plugin.py` 大幅瘦身，原来堆在主入口里的初始化、调度、路由和检索运行时逻辑被拆分出去。
- 新增 `core/runtime/*`，把生命周期、后台任务、请求去重、检索运行时构建做成独立层。
- 新增 `core/config/plugin_config_schema.py`，配置 schema 与 section 描述从主入口解耦。

### 2. 查询链路升级为可编排形态

- `components/tools/knowledge_query_tool.py` 从单文件重逻辑改成 orchestrator + mode handler。
- 新增 `query_modes_entity/person/relation` 与 `query_tool_orchestrator.py`，把实体、人设、关系、forward/time/episode 分支拆开。
- 新增 `core/utils/aggregate_query_service.py`，支持 `search/time/episode` 并发执行和 Weighted RRF 混合结果。
- 新增 `core/retrieval/graph_relation_recall.py`，对关系查询补图召回与路径证据。

### 3. Episode 情景记忆成为独立能力

- `core/storage/metadata_store.py` schema 升到 `SCHEMA_VERSION = 7`。
- 新增 `episodes`、`episode_paragraphs`、`episode_pending_paragraphs`、`episode_rebuild_sources` 等表和索引。
- 新增 `core/utils/episode_service.py`、`episode_segmentation_service.py`、`episode_retrieval_service.py`，打通 pending -> 分组 -> 语义切分 -> 落库 -> 检索。
- `components/commands/query_command.py` 与 `server.py` 都新增了 `episode` / `aggregate` 相关入口和接口。

### 4. 运维面从“运行时兼容”转为“离线迁移 + 自检 + 调优”

- 新增 `scripts/release_vnext_migrate.py`，明确要求离线做 preflight / migrate / verify。
- 新增 `core/utils/runtime_self_check.py` 与 `scripts/runtime_self_check.py`，启动与导入前都能真实探测 embedding 维度。
- 新增 `core/utils/retrieval_tuning_manager.py` 与 `web/tuning.html`，提供 Web 检索调优中心。
- `server.py` 新增 `/api/retrieval_tuning/*`、`/api/runtime/self_check*`、`/api/episodes/*` 等接口。

### 5. 数据语义与导入策略收紧

- `core/storage/knowledge_types.py`、`type_detection.py`、`summary_importer.py` 对知识类型做了重新建模。
- `knowledge_type` 允许值扩展并规范到 `structured / narrative / factual / quote / mixed`。
- README 与配置说明也已经切换到 vNext 语义，例如 `tool_search_mode` 不再强调 `legacy`，`embedding.quantization_type` 限定为 `int8/SQ8`。

## 6. 还有点想说的
  总而言之，感谢各位对于A_memorix的支持！本次V1.0.0的更新对于A_memorix来说是至关重要的里程碑！希望未来我们会走的更远！