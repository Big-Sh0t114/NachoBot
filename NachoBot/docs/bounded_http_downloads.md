# 二进制 HTTP 下载限额

`src/common/http_download.py` 提供一个轻量共享读取函数，供调用方已有的 HTTPX 流式响应复用。调用方用 `async with client.stream(...)` 持有响应，按原有策略检查状态码；读取完整且未超限后才保存或返回。异常与取消会退出流上下文并关闭响应。

当前边界为：沙盒 URL 文件 **1 MiB**、URL 视频感知 **64 MiB**、普通本地 TTS 音频 **16 MiB**。读取按最多 64 KiB 的解码后字节块进行；达到上限可接受，超限块会在加入累计缓冲前拒绝。有效的 `Content-Length` 只用于提前拒绝明显过大的未压缩响应；缺失、无效或偏小的值仍按实际字节计数。gzip 等编码按解码后的字节计数，不因压缩长度拒绝有效内容。不会保存或返回部分内容。

该边界控制应用层累计缓冲及超限后的继续读取；它不保证第三方解码器或进程 RSS 的严格峰值。调用继续使用现有 HTTPX 超时，没有新增重试、客户端池或全局服务。

## 验证

在 `NachoBot` 目录运行：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_bounded_http_downloads.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_tts_stream_*.py" -v
```

这两组定向测试分别覆盖真实 HTTPX 流式响应及三个调用方（16 项），以及既有 TTS 流测试（12 项）。
