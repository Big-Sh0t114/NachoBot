# NachoBot NapCat Adapter

The adapter connects NapCat's reverse OneBot WebSocket to NachoBot Core. Active offline-history recovery is enabled by default and uses NapCat's existing `get_friend_msg_history` action for private chats; it does not patch or replace NapCat.

On a verified QQ login, the adapter saves a first-enable baseline before waiting for Core. Messages older than that baseline are skipped. After a QQ or Core WebSocket reconnect, the adapter discovers allowed private sessions, pulls history in bounded pages, then passes recovered events through the ordinary admission, segment/media conversion, and Core send path. The original platform timestamp is retained. Live and recovered private messages share a stable scoped delivery key, and the adapter advances its durable per-session checkpoint only after Core confirms the exact recovery row. For incoming friend messages with a verified numeric account and peer, matching sender, original platform time, and positive `real_seq`, Core also receives the canonical QQNT sequence and the exact derived SnowLuma private-message ID. This lets Core resume or acknowledge a message already delivered by SnowLuma while preserving the original adapter key and receipt. NapCat's normalized `message_seq` is never used as QQNT sequence proof. Core shares only this verified private-message identity; it does not compare text or timestamps alone. Backend pagination cursors, checkpoints, and outboxes remain separate. Group messages continue through ordinary live delivery and NapCat's automatic repush; the adapter does not actively pull group history.

Recovery obtains one bounded `get_recent_contact(count=100)` navigation snapshot when needed; automatic retries may reuse it or run without another snapshot API call. Only matching friend C2C entries (`chatType=1`) can anchor an allowed private target; group and unsupported temporary-contact entries are ignored. A validated live private message supplies its normalized message ID and original timestamp directly as that peer's anchor, so a targeted live check needs no fresh recent-contact query, including after the global snapshot expires. For peers without a live anchor, if the ordinary latest history page fails, is empty, or is older than the available snapshot, the adapter retries from the snapshot's raw `msgId` through `get_friend_msg_history`. The snapshot message itself is never staged. When `lastestMsg` has no normalized ID, the adapter resolves the raw ID with `get_msg` after the anchored page and requires the resolved private message, account, original time, and short ID to match a row in that page. A same-second page without an exact known ID also uses this anchored proof. Missing or invalid snapshot entries do not mean the history is empty: a valid ordinary page can still proceed, while a failed first page without a usable anchor leaves the session incomplete and its checkpoint unchanged.

Within one verified NapCat/Core socket generation, the initial recovery scans all currently admitted private targets. Later runs rediscover admission and select only peers signaled by validated private live messages plus peers the previous run still reported as failed. Repeated live cues for one peer coalesce to its latest original-time/ID anchor; cues arriving during a scan remain queued for the next run. Successfully recovered peers stay out of failure retries, and a live cue never restores a full scan. A successful, validated recent-contact navigation snapshot is retained in memory for up to 300 seconds and filtered to the selected peers that still need a snapshot anchor; live overlays neither modify nor refresh that shared cache. Failed or malformed snapshots are not cached. Repeated failures without progress use the configured retry delay, then back off exponentially up to `max(300, retry_delay_sec)` seconds. A missing recent-contact entry or failed history page remains incomplete and never advances that session's checkpoint.

If one private session fails three consecutive recovery runs in the same QQ/Core connection generation, automatic retries stop for that session and a warning identifies it. A conversion error or missing Core delivery receipt counts once for that session in the run; the failed row is not resent within that run and remains pending. The session remains incomplete with its checkpoint unchanged; other admitted sessions continue recovering. Live messages do not reactivate an exhausted session. A new QQ or Core connection generation starts fresh retry counts and discards live anchors retained by the prior generation.

NapCat's friend-history response may also contain C2C temporary-session rows marked `message_type=private` and `sub_type=group`. These rows remain private-history navigation boundaries, but the ordinary adapter explicitly filters that unsupported subtype, so recovery never emits them as friend messages.

Recovery state lives in `data/offline_history.sqlite3`. It stores the baseline, per-session checkpoints, and a durable retry outbox; keep this file with the adapter across restarts. Imports and bridge construction do not open or create the database. A failed API page, conversion, stale connection, or missing Core receipt leaves the affected session incomplete for a later retry. Existing whitelist, blacklist, global-ban, QQ-bot, and self-mute admission checks remain in effect. For forwards, the adapter converts valid embedded OneBot nodes first, preserving all supported segments in node order; missing or empty embedded content retains the `get_forward_msg` fallback, and inner reply IDs are not looked up again.

The shared identity is limited to incoming friend private messages with the verified account, matching sender/peer, and matching original timestamp. NapCat's `real_seq` may arrive as a positive decimal string and is parsed losslessly within the QQNT uint32 range. Existing SnowLuma rows can be adopted only when their stored SnowLuma scope, derived ID, route, account, and original timestamp all match exactly. Rows without that proof keep the existing adapter-local behavior.

During a registered NapCat history run, each strictly validated private-history page may also send bounded identity-only records for incoming friend messages with valid `real_seq`. Core uses those records only to attach aliases to an already-pending or completed NapCat receipt with the exact backend/account/private peer/message key and original timestamp; it does not create a receipt or message row or change delivery state. This includes a scanned checkpoint-boundary row, allowing a later SnowLuma scan to recognize an older NapCat receipt even when NapCat's local checkpoint means it will not emit that message again. Missing proof or rows outside the verified scan remain backend-local. Identity seeding is best-effort and does not block or change the normal begin/seal receipt or checkpoint flow.

The migration does not delete or merge duplicate Core message rows already stored before an exact identity proof becomes available.

Configure the optional template section:

```toml
[offline_recovery]
enabled = true
page_size = 50
max_pages = 200
retry_delay_sec = 15.0
receipt_timeout_sec = 8.0
```

Omitting `[offline_recovery]` uses these defaults, so an existing live `config.toml` does not need a migration. Set `enabled = false` to disable active pulls. `page_size` bounds each API request; `max_pages` bounds a session scan. A capped or malformed scan does not emit a partial session batch or advance its checkpoint.

Read-only probes against the installed NapCat version confirmed the recent-contact, raw-ID history cursor, and `get_msg` coverage-proof API paths. A restart followed by end-to-end recovery and the resulting Core thinking/reply behavior have not been verified against a live QQ conversation.

## QQCall 共享适配层

QQCall 只由 `../NachoBot/config/bot_config.toml` 中 `[qqcall].enabled` 控制；连接参数保存在 `../NachoBot/src/plugins/built_in/qqcall_plugin/config.toml`。适配器、音频和 supervisor 都读取这份固定配置。启用后，适配器复用已验证登录身份、现有 Core `qq` 路由和 NapCat 当前的私聊/群聊准入策略；每次回调都会重新检查策略，不维护单独的 QQCall 白名单或开关。适配器不会创建第二条 QQ 连接，也不自动登录或接听。来电仍要求中央配置中的 UID→QQ UIN 映射及 `kernel_from_uid_matched` 关联。

在 canonical TOML `../NachoBot/src/plugins/built_in/qqcall_plugin/config.toml` 中管理 QQCall；此适配器模板不提供本地副本或独立开关。

共享 Runtime 通过本机认证 HTTP 控制服务获取有界事件，再把已关联的来电/接通/终态作为 senderless `qq.call.*` system event 送入 Core。显式 `qqcall_request` 才会调用接听；HTTP 返回的 `posted` 是控制服务的提交回执，不是 QQ SDK 接听确认。

音频状态严格跟随 canonical `bot_config.toml 的 [qqcall].enabled`，不再单独开关。启用后，即使没有手动设备名称，共享 Runtime 也会发布 QQCall 心跳并创建可选 streaming-ASR client；音频端点仍须通过 native guard 验证后才可使用。`[qqcall.audio]` 可选，只支持 `capture_device` 与 `playback_device` 两个名称覆盖项，两者可分别省略或留空。supervisor 会把已验证的 guard 设备 ID 固定到对应名称；采集固定为 loopback，播放固定为 virtual，不会默认打开物理麦克风。缺少 guard 产物或私有 guard 配置时音频保持 unavailable。默认 `engine = "universalvc"`，惰性导入仓库内 `NachoBot-UniversalVC-Adapter` 的 Silero `VADProcessor` 与 WAV 编码 helper，不复制其处理算法；每通新建 VAD 实例，使用 400 ms pre-roll、250 ms 最短语音和 500 ms 静音结束。UniversalVC 依赖、模型缺失或初始化失败时音频保持 unavailable，不会静默退回 RMS。入站音频经有界 VAD 分段后优先使用 Core 的 QQ streaming-ASR API，流服务不可用或结果无效时把同一段 PCM16 作为普通 Core voice 消息交给现有 voice 模型选择。Core 的 TTS 文本和 PCM 流由适配器本地消费，不会作为普通 QQ 消息发送；播放成功只表示选定本地渲染端点已排空，不表示远端已听到。

PPAPI readiness 本身不证明音频路由：Runtime 还要求当前 Host generation 对应的 Windows 活跃会话证据，并每秒复核；缺失、过期或端点名不匹配会撤销音频 ready 状态和当前 turn fence。Core streaming-ASR 使用现有 Core route token；没有 token 时，仅允许通过 Router 已配置的 loopback HTTP origin 访问无认证本机 Core，远程 origin 会 fail closed。本次自动设备检测改动尚未进行真实 QQ/NapCat 通话验收。

Windows adapter 基础依赖包含 PyAudioWPatch；Python 3.13+ 使用 `audioop-lts`。`bot_config.toml 的 [qqcall].enabled = false` 时适配器不会打开或探测音频设备。启用 UniversalVC VAD 前，在 NapCat adapter 目录执行 `uv sync --extra qqcall-audio`，以安装锁定的 NumPy、sherpa-onnx 与 ONNX Runtime；依赖导入保持惰性。
