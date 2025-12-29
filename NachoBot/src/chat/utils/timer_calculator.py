import asyncio

from time import perf_counter
from functools import wraps
from typing import Optional, Dict, Callable
from rich.traceback import install

install(extra_lines=3)

"""
# 更好的计时器

使用形式：
- 上下文
- 装饰器
- 直接实例化

使用场景：
- 使用Timer：在需要测量代码执行时间时（如性能测试、计时器工具），Timer类是更可靠、高精度的选择。
- 使用time.time()的场景：当需要记录实际时间点（如日志、时间戳）时使用，但避免用它测量时间间隔。

使用方式：

【装饰器】
time_dict = {}
@Timer("计数", time_dict)
def func():
    pass
print(time_dict)

【上下文_1】
def func():
    with Timer() as t:
        pass
    print(t)
    print(t.human_readable)

【上下文_2】
def func():
    time_dict = {}
    with Timer("计数", time_dict):
        pass
    print(time_dict)

【直接实例化】
a = Timer()
print(a)         # 直接输出当前 perf_counter 值

参数：
- name：计时器的名字，默认为 None
- storage：计时器结果存储字典，默认为 None
- auto_unit：自动选择单位（毫秒或秒），默认为 True（自动根据时间切换毫秒或秒）
- do_type_check：是否进行类型检查，默认为 False（不进行类型检查）
    
属性：human_readable

自定义错误：TimerTypeError
"""


class TimerTypeError(TypeError):
    """自定义类型错误"""

    __slots__ = ()

    def __init__(self, param, expected_type, actual_type):
        super().__init__(f"参数 '{param}' 类型错误，期望 {expected_type}，实际得到 {actual_type.__name__}")


class Timer:
    """
    Timer 支持三种模式：
      1. 装饰器模式：用于测量函数/协程运行时间
      2. 上下文管理器模式：用于 with 语句块内部计时
      3. 直接实例化：如果不调用 __enter__，打印对象时将显示当前 perf_counter 的值
    """

    __slots__ = ("name", "storage", "elapsed", "auto_unit", "start")

    def __init__(
        self,
        name: Optional[str] = None,
        storage: Optional[Dict[str, float]] = None,
        auto_unit: bool = True,
        do_type_check: bool = False,
    ):
        if do_type_check:
            self._validate_types(name, storage)

        self.name = name
        self.storage = storage
        self.elapsed: float = None  # type: ignore

        self.auto_unit = auto_unit
        self.start: float = None  # type: ignore

    @staticmethod
    def _validate_types(name, storage):
        """类型检查"""
        if name is not None and not isinstance(name, str):
            raise TimerTypeError("name", "Optional[str]", type(name))

        if storage is not None and not isinstance(storage, dict):
            raise TimerTypeError("storage", "Optional[dict]", type(storage))

    def __call__(self, func: Optional[Callable] = None) -> Callable:
        """装饰器模式"""
        if func is None:
            return lambda f: Timer(name=self.name or f.__name__, storage=self.storage, auto_unit=self.auto_unit)(f)

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            with self:
                return await func(*args, **kwargs)
            return None

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)
            return None

        wrapper = async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
        wrapper.__timer__ = self  # 保留计时器引用 # type: ignore
        return wrapper

    def __enter__(self):
        """上下文管理器入口"""
        self.start = perf_counter()
        return self

    def __exit__(self, *args):
        self.elapsed = perf_counter() - self.start
        self._record_time()
        return False

    def _record_time(self):
        """记录时间"""
        if self.storage is not None and self.name:
            self.storage[self.name] = self.elapsed

    @property
    def human_readable(self) -> str:
        """人类可读时间格式"""
        if self.elapsed is None:
            return "未计时"

        if self.auto_unit:
            return f"{self.elapsed * 1000:.2f}毫秒" if self.elapsed < 1 else f"{self.elapsed:.2f}秒"
        return f"{self.elapsed:.4f}秒"

    def __str__(self):
        if self.start is not None:
            if self.elapsed is None:
                current_elapsed = perf_counter() - self.start
                return f"<Timer {self.name or '匿名'} [计时中: {current_elapsed:.4f}秒]>"
            return f"<Timer {self.name or '匿名'} [{self.human_readable}]>"
        return f"{perf_counter()}"
