import asyncio
import json
import uuid
import random
from collections.abc import Callable, Mapping
import websockets as Server
from ncnk_message import MessageBase
from ncnk_message.external_self_message import outbound_echo_registry

from src.response_pool import get_response
from src.logger import logger
from src.recv_handler.message_sending import message_send_instance


class NCMessageSender:
    def __init__(self):
        self.server_connection: Server.ServerConnection = None
        self._send_queue = asyncio.Queue()  # 全局消息队列
        self._worker_task = None
        self._echo_scope_provider: Callable[[], tuple[str, str, str] | None] | None = None

    def configure_outbound_echo_tracking(
        self,
        scope_provider: Callable[[], tuple[str, str, str] | None],
    ) -> None:
        """Bind the live platform/generation/verified-account identity source."""

        self._echo_scope_provider = scope_provider

    @staticmethod
    def _message_action_route(action: str, params: dict) -> tuple[str, str] | None:
        name = str(action or "").strip().lower()
        values = params if isinstance(params, Mapping) else {}
        private_actions = {
            "send_private_msg",
            "send_private_forward_msg",
            "upload_private_file",
            "send_private_file",
        }
        group_actions = {
            "send_group_msg",
            "send_group_forward_msg",
            "upload_group_file",
            "send_group_file",
        }
        if name in private_actions:
            peer = values.get("user_id", values.get("target_id"))
            kind = "private"
        elif name in group_actions:
            peer = values.get("group_id", values.get("target_id"))
            kind = "group"
        elif name in {"send_msg", "send_forward_msg"}:
            kind = str(values.get("message_type") or "").strip().lower()
            if kind == "group":
                peer = values.get("group_id", values.get("target_id"))
            elif kind == "private":
                peer = values.get("user_id", values.get("target_id"))
            else:
                return None
        else:
            return None
        if isinstance(peer, bool) or peer is None:
            return None
        peer_id = str(peer).strip()
        return (kind, peer_id) if peer_id else None

    @staticmethod
    def _message_id_from_response(response: object) -> str | None:
        if not isinstance(response, Mapping) or response.get("status") != "ok":
            return None
        data = response.get("data")
        value = data.get("message_id") if isinstance(data, Mapping) else response.get("message_id")
        if isinstance(value, bool) or value is None:
            return None
        message_id = str(value).strip()
        return message_id or None

    @staticmethod
    def _authoritative_action_failure(response: object) -> bool:
        if not isinstance(response, Mapping):
            return False
        status = str(response.get("status") or "").strip().lower()
        if status in {"failed", "error"}:
            return True
        if status and status != "ok":
            return True
        retcode = response.get("retcode")
        return isinstance(retcode, int) and not isinstance(retcode, bool) and retcode not in {0, 1}

    async def set_server_connection(self, connection: Server.ServerConnection):
        self.server_connection = connection
        # 当连接建立时，启动或重启发送队列的消费者任务
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._queue_worker())
            logger.info("已启动 NapCat 消息发送全局队列 Worker")

    async def _queue_worker(self):
        """后台任务：不断从队列取出消息发送，强制进行安全间隔休眠"""
        while True:
            try:
                (
                    payload,
                    request_uuid,
                    send_future,
                    expected_connection,
                    pre_send_check,
                    identity_check,
                    message_route,
                    timeout_value,
                ) = await self._send_queue.get()
                echo_token = None
                try:
                    if send_future.cancelled():
                        continue
                    if expected_connection is None or self.server_connection is not expected_connection:
                        logger.debug("跳过已过期NapCat请求 UUID={}", request_uuid)
                        if not send_future.done():
                            send_future.set_result({"status": "error", "message": "stale_connection"})
                        continue
                    if pre_send_check is not None:
                        try:
                            allowed = await asyncio.wait_for(pre_send_check(), timeout=5.0)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            allowed = False
                        if allowed is not True:
                            if not send_future.done():
                                send_future.set_result({"status": "error", "message": "pre_send_check_failed"})
                            continue
                        if send_future.cancelled():
                            continue
                        try:
                            identity_current = identity_check is None or identity_check() is True
                        except Exception:
                            identity_current = False
                        if not identity_current or self.server_connection is not expected_connection:
                            if not send_future.done():
                                send_future.set_result({"status": "error", "message": "stale_connection"})
                            continue
                    logger.debug(f"[Queue] 执行发送任务: {request_uuid}")
                    if message_route is not None and self._echo_scope_provider is not None:
                        try:
                            scope = self._echo_scope_provider()
                            if isinstance(scope, tuple) and len(scope) == 3:
                                echo_token = outbound_echo_registry.register(
                                    *scope,
                                    *message_route,
                                    timeout_sec=timeout_value,
                                )
                        except Exception as exc:
                            logger.debug("NapCat outbound echo registration skipped error={}", str(exc)[:160])
                    await expected_connection.send(payload)
                    if not send_future.done():
                        send_future.set_result(
                            {"status": "ok", "_outbound_echo_token": echo_token}
                        )
                    elif echo_token is not None:
                        outbound_echo_registry.mark_uncertain(echo_token)
                        echo_token = None
                    delay = 1.5 + random.uniform(0.5, 1.5)
                    logger.debug(f"[Queue] 发送完毕，休眠 {delay:.2f} 秒以规避风控...")
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    if echo_token is not None:
                        outbound_echo_registry.mark_uncertain(echo_token)
                    if not send_future.done():
                        send_future.set_result({"status": "error", "message": "cancelled"})
                    raise
                except Exception as e:
                    if echo_token is not None:
                        outbound_echo_registry.mark_uncertain(echo_token)
                    if not send_future.done():
                        send_future.set_result({"status": "error", "message": str(e)})
                    logger.error(f"[Queue] 发送 Worker 发生异常: {e}")
                    await asyncio.sleep(1)
                finally:
                    self._send_queue.task_done()
            except Exception as e:
                logger.error(f"[Queue] 发送 Worker 发生异常: {e}")
                await asyncio.sleep(1) # 出错也稍微等一下，避免死循环爆日志

    async def send_message_to_napcat(
        self,
        action: str,
        params: dict,
        *,
        timeout_sec: float | None = None,
        expected_connection: Server.ServerConnection | None = None,
        pre_send_check=None,
        identity_check=None,
    ) -> dict:
        request_uuid = str(uuid.uuid4())
        payload = json.dumps({"action": action, "params": params, "echo": request_uuid})
        timeout_value = 10.0 if timeout_sec is None else max(1.0, float(timeout_sec))
        message_route = self._message_action_route(action, params)
        tracking_token = None
        connection = expected_connection or self.server_connection
        if connection is None:
            return {"status": "error", "message": "no_connection"}

        # 创建一个 Future 用于等待 NapCat 的 echo 返回
        loop = asyncio.get_running_loop()
        send_future = loop.create_future()

        # 将任务塞入队列
        await self._send_queue.put(
            (
                payload,
                request_uuid,
                send_future,
                connection,
                pre_send_check,
                identity_check,
                message_route,
                timeout_value,
            )
        )

        # 等待 get_response 的返回（由底层的 ws 接收循环触发 response_pool 里的事件）
        try:
            send_result = await asyncio.wait_for(asyncio.shield(send_future), timeout=timeout_value)
            if not isinstance(send_result, dict) or send_result.get("status") != "ok":
                return send_result if isinstance(send_result, dict) else {"status": "error", "message": "send_failed"}
            # Worker 只负责推送并卡住节奏，接收回执仍由 src.response_pool 处理。
            tracking_token = send_result.get("_outbound_echo_token")
            try:
                response = await get_response(request_uuid, timeout=timeout_value)
                if tracking_token is not None:
                    message_id = self._message_id_from_response(response)
                    if message_id is not None:
                        outbound_echo_registry.resolve(tracking_token, message_id, success=True)
                    elif self._authoritative_action_failure(response):
                        outbound_echo_registry.resolve(tracking_token, success=False)
                    else:
                        outbound_echo_registry.mark_uncertain(tracking_token)
                    tracking_token = None
            finally:
                if tracking_token is not None:
                    outbound_echo_registry.mark_uncertain(tracking_token)
                    tracking_token = None
        except TimeoutError:
            if not send_future.done():
                send_future.cancel()
            logger.error(
                f"发送消息超时，未收到响应 UUID: {request_uuid}, timeout_sec={timeout_value}"
            )
            return {"status": "error", "message": "timeout"}
        except asyncio.CancelledError:
            if not send_future.done():
                send_future.cancel()
            raise
        except Exception as e:
            logger.error(f"等待消息回执失败: {e}")
            return {"status": "error", "message": str(e)}

        return response

    async def message_sent_back(self, message_base: MessageBase, qq_message_id: str) -> None:
        # # 修改 additional_config，添加 echo 字段
        # if message_base.message_info.additional_config is None:
        #     message_base.message_info.additional_config = {}

        # message_base.message_info.additional_config["echo"] = True

        # # 获取原始的 mmc_message_id
        # mmc_message_id = message_base.message_info.message_id

        # # 修改 message_segment 为 notify 类型
        # message_base.message_segment = Seg(
        #     type="notify", data={"sub_type": "echo", "echo": mmc_message_id, "actual_id": qq_message_id}
        # )
        # await message_send_instance.message_send(message_base)
        # logger.debug("已回送消息ID")
        # return
        platform = message_base.message_info.platform
        mmc_message_id = message_base.message_info.message_id
        echo_data = {
            "type": "echo",
            "echo": mmc_message_id,
            "actual_id": qq_message_id,
        }
        success = await message_send_instance.send_custom_message(echo_data, platform, "message_id_echo")
        if success:
            logger.debug("已回送消息ID")
        else:
            logger.error("回送消息ID失败")


nc_message_sender = NCMessageSender()
