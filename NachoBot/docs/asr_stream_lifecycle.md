# Core 流式 ASR 生命周期

Core 为每个已登记的 ASR 会话保留一个互斥锁；chunk 和 finish 的运行时请求由独立子任务执行。会话被取消、显式 abort、过期清理或 shutdown 时，路由器先在会话注册表锁内注销会话，再通知等待中的请求并取消运行时子任务。该过程不等待会话锁，因此不会被正在等待模型响应的 chunk/finish 阻塞。

一次会话注销只创建一个运行时 abort 清理任务。它在有限时间内尝试 abort，并在成功或失败后释放模型租约；完成路径则在同一个注册表临界区内注销会话并写入 ASR 回执。会话注销后，操作的迟到结果不会推进序号、覆盖 partial 文本或创建 ASR 回执。shutdown 先注销会话并取消运行时操作，再排空可协作任务和清理任务，最后关闭传输。

Discord、Bilibili、UniversalVC 和 WebUI 继续复用 `start_audio_stream`、`append_audio_stream_chunk`、`finish_audio_stream`、`abort_audio_stream`。此生命周期属于 Core 共享能力，不增加平台分支或 HTTP 字段。取消可以中止正常可取消的 HTTP 请求；如果运行时或原生/GPU 推理忽略取消，Core 只能安全丢弃迟到结果，不能保证推理本身已停止。shutdown 对运行时操作作有限排空，仍未结束的任务由路由器继续持有；正常 httpx 取消会及时结束。

验证：在 `NachoBot` 目录运行 `.venv/Scripts/python.exe -m unittest discover -s tests -p test_asr_stream_lifecycle.py -v`，并运行 `test_multimodal_core.py`、`test_multimodal_api.py` 和 `ruff check src/multimodal/router.py tests/test_asr_stream_lifecycle.py`。
