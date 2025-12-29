from src.common.logger import get_logger
from src.plugin_system.apis import send_api

"""
视线管理系统使用说明：

1. 视线状态：
   - wandering: 随意看
   - danmu: 看弹幕
   - lens: 看镜头

2. 状态切换逻辑：
   - 收到消息时 → 切换为看弹幕，立即发送更新
   - 开始生成回复时 → 切换为看镜头或随意，立即发送更新  
   - 生成完毕后 → 看弹幕1秒，然后回到看镜头直到有新消息，状态变化时立即发送更新

3. 使用方法：
   # 获取视线管理器
   watching = watching_manager.get_watching_by_chat_id(chat_id)
   
   # 收到消息时调用
   await watching.on_message_received()
   
   # 开始生成回复时调用
   await watching.on_reply_start()
   
   # 生成回复完毕时调用
   await watching.on_reply_finished()

4. 自动更新系统：
   - 状态变化时立即发送type为"watching"，data为状态值的websocket消息
   - 使用定时器自动处理状态转换（如看弹幕时间结束后自动切换到看镜头）
   - 无需定期检查，所有状态变化都是事件驱动的
"""

logger = get_logger("watching")

HEAD_CODE = {
    "看向上方": "(0,0.5,0)",
    "看向下方": "(0,-0.5,0)",
    "看向左边": "(-1,0,0)",
    "看向右边": "(1,0,0)",
    "随意朝向": "random",
    "看向摄像机": "camera",
    "注视对方": "(0,0,0)",
    "看向正前方": "(0,0,0)",
}


class ChatWatching:
    def __init__(self, chat_id: str):
        self.chat_id: str = chat_id

    async def on_reply_start(self):
        """开始生成回复时调用"""
        await send_api.custom_to_stream(
            message_type="state", content="start_thinking", stream_id=self.chat_id, storage_message=False
        )

    async def on_reply_finished(self):
        """生成回复完毕时调用"""
        await send_api.custom_to_stream(
            message_type="state", content="finish_reply", stream_id=self.chat_id, storage_message=False
        )

    async def on_thinking_finished(self):
        """思考完毕时调用"""
        await send_api.custom_to_stream(
            message_type="state", content="finish_thinking", stream_id=self.chat_id, storage_message=False
        )

    async def on_message_received(self):
        """收到消息时调用"""
        await send_api.custom_to_stream(
            message_type="state", content="start_viewing", stream_id=self.chat_id, storage_message=False
        )

    async def on_internal_message_start(self):
        """收到消息时调用"""
        await send_api.custom_to_stream(
            message_type="state", content="start_internal_thinking", stream_id=self.chat_id, storage_message=False
        )


class WatchingManager:
    def __init__(self):
        self.watching_list: list[ChatWatching] = []
        """当前视线状态列表"""
        self.task_started: bool = False

    def get_watching_by_chat_id(self, chat_id: str) -> ChatWatching:
        """获取或创建聊天对应的视线管理器"""
        for watching in self.watching_list:
            if watching.chat_id == chat_id:
                return watching

        new_watching = ChatWatching(chat_id)
        self.watching_list.append(new_watching)
        logger.info(f"为chat {chat_id}创建新的视线管理器")

        return new_watching


# 全局视线管理器实例
watching_manager = WatchingManager()
"""全局视线管理器"""
