# 只读请求的并发合并

`src.common.async_singleflight.AsyncSingleFlight` 合并同一实例、同一事件循环内正在进行的相同只读请求。请求结束后立即移除记录；它不缓存完成结果，也不跨 `UrlContentFetcher` 或 `PlaywrightSearchProvider` 实例共享工作。不要用于发送消息、执行工具或其他有副作用的操作。

```python
flight = AsyncSingleFlight(max_inflight=32)
value = await flight.run(fingerprint_json("request", query, limit), load)
```

键必须是短字符串；`fingerprint_json` 只接受内置 JSON 标量和标量序列，并返回固定长度 SHA-256 键。不要把原始 URL 放进键。默认情况下，每个 helper 在每个事件循环最多跟踪 32 个底层操作，包含已从键表移除但仍未结束的取消抵抗任务。同键现有请求仍可加入；容量耗尽时，新键会收到 `SingleFlightCapacityError`。URL 抓取和搜索调用分别把容量错误转成 `None` 和空列表，并且只记录固定异常类型名。

每个调用者通过 `asyncio.shield` 等待：取消一个等待者不会取消其他人的工作；最后一个等待者离开时，旧键会先移除，再最多取消底层任务一次。取消抵抗的任务继续占用容量到真正结束，晚到异常会被取回。工厂在同一 helper 中再次调用 `run` 会收到固定的 `SingleFlightReentrancyError`，避免同键或跨键自等候死锁。返回对象本身可能被 helper 原样共享；搜索包装器会为每位调用者复制列表和字典，URL结果是不可变元组。

URL键包含 URL 及超时、字节/字符限制、最小文本长度、User-Agent、浏览器优先级和浏览器超时。搜索键包含去空白后的查询、结果数、引擎顺序、超时和 User-Agent。请在开始请求前设置实例配置；键不保证配置在请求进行中被修改时的行为。helper 为不同事件循环维护独立状态，但这不会让共享 Playwright 浏览器 runtime 跨事件循环可用。

## 验证

在 `NachoBot` 目录运行：

```powershell
py -3.12 -B -m unittest discover -s tests -p test_async_singleflight.py
py -3.12 -B -m unittest discover -s tests -p test_readonly_singleflight.py
py -3.12 -B -m unittest discover -s tests -p test_shared_browser_runtime.py
py -3.11 -B -m unittest discover -s tests -p test_async_singleflight.py
py -3.11 -B -m unittest discover -s tests -p test_readonly_singleflight.py
```

前两组 helper/consumer 测试使用真实类和安全导入桩，不导入应用配置、日志初始化或数据库模块；Python 3.12 下 consumer 组还会用本机回环 HTTP 服务验证真实 `aiohttp` 抓取和 HTML 提取。Python 3.11 环境缺少 `aiohttp` 时只跳过该回环用例。浏览器调用由桩测试覆盖，不启动 Chromium，也不访问外网或搜索 API。第三条是既有共享浏览器 runtime 回归套件。
