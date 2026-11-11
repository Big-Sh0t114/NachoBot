from abc import abstractmethod

import asyncio
import inspect
import math
from asyncio import Event, Lock, Task
from typing import Callable, Dict

from src.common.logger import get_logger

logger = get_logger("async_task_manager")


class AsyncTask:
    """异步任务基类"""

    def __init__(self, task_name: str | None = None, wait_before_start: int = 0, run_interval: int = 0):
        self.task_name: str = task_name or self.__class__.__name__
        """任务名称"""

        self.wait_before_start: int = wait_before_start
        """运行任务前是否进行等待（单位：秒，设为0则不等待）"""

        self.run_interval: int = run_interval
        """多次运行的时间间隔（单位：秒，设为0则仅运行一次）"""

    @abstractmethod
    async def run(self):
        """
        任务的执行过程
        """
        pass

    @staticmethod
    def _raise_if_cancelling():
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise asyncio.CancelledError

    async def _wait_or_abort(self, abort_flag: asyncio.Event, delay: float) -> bool:
        """Wait for a delay, returning early when the manager aborts this generation."""
        self._raise_if_cancelling()
        if abort_flag.is_set():
            return False

        try:
            async with asyncio.timeout(delay):
                await abort_flag.wait()
        except TimeoutError:
            return not abort_flag.is_set()
        return False

    async def start_task(self, abort_flag: asyncio.Event):
        self._raise_if_cancelling()
        if abort_flag.is_set():
            return

        if self.wait_before_start > 0:
            if not await self._wait_or_abort(abort_flag, self.wait_before_start):
                return

        while not abort_flag.is_set():
            self._raise_if_cancelling()
            if abort_flag.is_set():
                return

            await self.run()

            # A task may catch CancelledError inside run(). Do not let that suppress
            # manager cancellation and restart the periodic loop.
            self._raise_if_cancelling()
            if self.run_interval <= 0 or abort_flag.is_set():
                return
            if not await self._wait_or_abort(abort_flag, self.run_interval):
                return

            self._raise_if_cancelling()


class AsyncTaskManager:
    """Owns named periodic tasks and bounds replacement and shutdown waits."""

    def __init__(self, *, replacement_timeout: float = 5.0, shutdown_timeout: float = 10.0):
        self.replacement_timeout = self._validate_timeout("replacement_timeout", replacement_timeout)
        self.shutdown_timeout = self._validate_timeout("shutdown_timeout", shutdown_timeout)

        self.tasks: Dict[str, Task] = {}
        """Live task owners, indexed by the name captured when they were registered."""

        self.abort_flag: Event = Event()
        """Abort event for the current admission generation."""

        self._state_lock: Lock = Lock()
        # Keep the historical private spelling for code or diagnostics that inspect it.
        self._lock: Lock = self._state_lock
        self._generation = 0
        self._stopping = False
        self._shutdown_task: Task | None = None
        self._cancel_requested: set[Task] = set()
        self._name_coordinators: dict[str, tuple[Lock, int]] = {}

    @staticmethod
    def _validate_timeout(name: str, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite, nonnegative number")
        try:
            normalized = float(value)
        except (OverflowError, ValueError):
            raise ValueError(f"{name} must be a finite, nonnegative number") from None
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError(f"{name} must be a finite, nonnegative number")
        return normalized

    def _retain_name_coordinator(self, name: str) -> Lock:
        entry = self._name_coordinators.get(name)
        if entry is None:
            lock = Lock()
            self._name_coordinators[name] = (lock, 1)
            return lock

        lock, users = entry
        self._name_coordinators[name] = (lock, users + 1)
        return lock

    def _release_name_coordinator(self, name: str, lock: Lock):
        entry = self._name_coordinators.get(name)
        if entry is None or entry[0] is not lock:
            return
        users = entry[1] - 1
        if users <= 0:
            del self._name_coordinators[name]
        else:
            self._name_coordinators[name] = (lock, users)

    def _remove_task_call_back(self, task: Task, registered_key: str | None = None):
        """Remove this exact owner, never a newer task that reused its name."""
        if registered_key is not None:
            if self.tasks.get(registered_key) is task:
                del self.tasks[registered_key]
        else:
            for key, current in tuple(self.tasks.items()):
                if current is task:
                    del self.tasks[key]
                    break

        self._cancel_requested.discard(task)

    @staticmethod
    def _default_finish_call_back(task: Task):
        """Observe the task result even when a caller supplied its own callback."""
        try:
            task.result()
            logger.debug(f"任务 '{task.get_name()}' 完成")
        except asyncio.CancelledError:
            logger.debug(f"任务 '{task.get_name()}' 被取消")
        except Exception as e:
            logger.error(f"任务 '{task.get_name()}' 执行时发生异常: {e}", exc_info=True)

    def _finish_task(self, task: Task, registered_key: str, user_callback: Callable[[Task], None] | None):
        self._remove_task_call_back(task, registered_key)
        self._default_finish_call_back(task)

        if user_callback is None:
            return
        try:
            callback_result = user_callback(task)
            if inspect.isawaitable(callback_result):
                close = getattr(callback_result, "close", None)
                if close is not None:
                    close()
                logger.error("AsyncTaskManager completion callbacks must be synchronous")
        except asyncio.CancelledError:
            logger.error(f"任务 '{registered_key}' 的完成回调意外抛出取消异常", exc_info=True)
        except Exception as e:
            logger.error(f"任务 '{registered_key}' 的完成回调发生异常: {e}", exc_info=True)

    def _check_admission(self, generation: int, caller: Task | None, registered_key: str):
        if caller is not None and self._has_pending_cancellation(caller):
            raise asyncio.CancelledError
        if generation != self._generation or self._stopping:
            raise RuntimeError("task registration was invalidated by shutdown")
        if caller is not None and self.tasks.get(registered_key) is caller:
            raise RuntimeError(f"managed task '{registered_key}' cannot replace itself")

    @staticmethod
    def _has_pending_cancellation(task: Task) -> bool:
        if not task.cancelling():
            return False
        # `cancelling()` remains nonzero after CancelledError has been delivered.
        # During a worker's `finally` cleanup it is safe to register unrelated work;
        # `_must_cancel` distinguishes that state from a cancellation still pending
        # injection at an admission/publication boundary. Unknown Task implementations
        # conservatively fall back to rejecting admission.
        return bool(getattr(task, "_must_cancel", True))

    def _request_cancel_once(self, task: Task, task_name: str):
        if task.done() or task.cancelling() or task in self._cancel_requested:
            return
        self._cancel_requested.add(task)
        try:
            task.cancel()
            logger.debug(f"已请求取消任务 '{task_name}'")
        except Exception as e:
            self._cancel_requested.discard(task)
            logger.warning(f"取消任务 '{task_name}' 时发生异常: {e}")

    async def _wait_for_replaced_task(self, old_task: Task, task_name: str):
        if old_task.done():
            return
        done, _ = await asyncio.wait((old_task,), timeout=self.replacement_timeout)
        if not done:
            logger.warning(f"等待任务 '{task_name}' 完成超时")
            raise TimeoutError(f"task '{task_name}' did not finish within {self.replacement_timeout:g}s")

    async def add_task(self, task: AsyncTask, call_back: Callable[[asyncio.Task], None] | None = None):
        """Register a task, replacing its previous generation only after it exits."""
        if not isinstance(task, AsyncTask):
            raise TypeError(f"task '{task.__class__.__name__}' 必须是继承 AsyncTask 的子类")

        registered_key = task.task_name
        if not isinstance(registered_key, str):
            raise TypeError("task.task_name must be a string")

        caller = asyncio.current_task()
        generation = self._generation  # Capture before any per-name wait.
        self._check_admission(generation, caller, registered_key)

        coordinator = self._retain_name_coordinator(registered_key)
        acquired = False
        try:
            await coordinator.acquire()
            acquired = True

            # The registry may have changed while this call waited for its name.
            self._check_admission(generation, caller, registered_key)
            old_task = self.tasks.get(registered_key)
            if old_task is not None:
                # This second identity check also catches eager task execution that
                # entered add_task before its outer create_task call was published.
                if old_task is caller:
                    raise RuntimeError(f"managed task '{registered_key}' cannot replace itself")

                self._request_cancel_once(old_task, registered_key)
                await self._wait_for_replaced_task(old_task, registered_key)

            async with self._state_lock:
                self._check_admission(generation, caller, registered_key)
                previous = self.tasks.get(registered_key)
                if previous is not None:
                    if previous is caller:
                        raise RuntimeError(f"managed task '{registered_key}' cannot replace itself")
                    if not previous.done():
                        # Shutdown or an unexpected publisher retained an owner while
                        # this call waited. Never overlap it with a replacement.
                        raise TimeoutError(f"task '{registered_key}' is still running")
                    self._remove_task_call_back(previous, registered_key)

                self._check_admission(generation, caller, registered_key)
                runner = task.start_task(self.abort_flag)
                try:
                    task_inst = asyncio.create_task(runner, name=registered_key)
                except BaseException:
                    runner.close()
                    raise

                # Do not await between task creation and ownership/callback setup.
                self.tasks[registered_key] = task_inst
                task_inst.add_done_callback(
                    lambda completed, key=registered_key, callback=call_back: self._finish_task(
                        completed, key, callback
                    )
                )
                logger.debug(f"已启动任务 '{registered_key}'")
                if caller is not None and self._has_pending_cancellation(caller):
                    # A task factory can synchronously cancel the registering caller.
                    # The child is already created (and an eager factory may have
                    # started it), so retain it, observe it, request cancellation once,
                    # and let the captured-key callback release ownership when done.
                    self._request_cancel_once(task_inst, registered_key)
                    raise asyncio.CancelledError
        finally:
            if acquired:
                coordinator.release()
            self._release_name_coordinator(registered_key, coordinator)

    def get_tasks_status(self) -> Dict[str, Dict[str, str]]:
        """Return live registered owners, including workers draining cancellation."""
        result: Dict[str, Dict[str, str]] = {}
        for task_name, task in self.tasks.items():
            if task.done():
                status = "done"
            elif self._stopping or task.cancelling():
                status = "stopping"
            else:
                status = "running"
            result[task_name] = {"status": status}
        return result

    @staticmethod
    def _observe_shutdown_task(task: Task):
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning("AsyncTaskManager shutdown operation was cancelled")
        except Exception as e:
            logger.error(f"AsyncTaskManager shutdown operation failed: {e}", exc_info=True)

    async def _finish_shutdown(self, task_items: tuple[tuple[str, Task], ...]):
        current = asyncio.current_task()
        try:
            for task_name, task in task_items:
                self._request_cancel_once(task, task_name)

            pending = {task for _, task in task_items if not task.done()}
            if pending:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.shutdown_timeout
                _, pending = await asyncio.wait(pending, timeout=max(0.0, deadline - loop.time()))

            if pending:
                names = sorted(task.get_name() for task in pending)
                logger.warning(f"异步任务超过统一关闭时限，仍在结束中: {', '.join(names)}")
            else:
                logger.info("所有异步任务已停止")
        finally:
            async with self._state_lock:
                # Old workers keep their old, set event. New admissions get a fresh
                # event, and retained workers remain in tasks until their callbacks run.
                self.abort_flag = Event()
                self._stopping = False
                if self._shutdown_task is current:
                    self._shutdown_task = None

    async def stop_and_wait_all_tasks(self):
        """Close admission and join one shielded, globally bounded shutdown operation."""
        operation = self._shutdown_task
        if operation is None or operation.done():
            async with self._state_lock:
                operation = self._shutdown_task
                if operation is None or operation.done():
                    self._stopping = True
                    self.abort_flag.set()
                    task_items = tuple(self.tasks.items())
                    self._generation += 1
                    shutdown_coroutine = self._finish_shutdown(task_items)
                    try:
                        operation = asyncio.create_task(
                            shutdown_coroutine, name="async-task-manager-shutdown"
                        )
                    except BaseException:
                        # A custom task factory can fail before taking ownership of
                        # this coroutine. Keep admission closed only for the atomic
                        # transition, cancel the captured owners once, and recover
                        # with a fresh generation event before propagating the error.
                        shutdown_coroutine.close()
                        for task_name, task in task_items:
                            self._request_cancel_once(task, task_name)
                        self.abort_flag = Event()
                        self._stopping = False
                        raise
                    operation.add_done_callback(self._observe_shutdown_task)
                    self._shutdown_task = operation

        await asyncio.shield(operation)

    def debug_task_status(self):
        """Log manager-owned tasks and event-loop task counts for diagnostics."""
        logger.info("=== 异步任务状态调试信息 ===")
        logger.info(f"当前管理的任务数量: {len(self.tasks)}")
        logger.info(f"中止标志状态: {self.abort_flag.is_set()}")

        for task_name, task in self.tasks.items():
            status = []
            if task.done():
                status.append("已完成")
                if task.cancelled():
                    status.append("已取消")
                elif task.exception():
                    status.append(f"异常: {task.exception()}")
                else:
                    status.append("正常完成")
            elif task.cancelling() or self._stopping:
                status.append("正在停止")
            else:
                status.append("运行中")

            logger.info(f"任务 '{task_name}': {', '.join(status)}")

        all_tasks = asyncio.all_tasks()
        logger.info(f"当前事件循环中的所有任务数量: {len(all_tasks)}")
        logger.info("=== 调试信息结束 ===")


async_task_manager = AsyncTaskManager()
"""全局异步任务管理器实例"""
