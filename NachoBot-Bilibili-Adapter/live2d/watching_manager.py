import asyncio


class ChatWatching:
    def __init__(self, chat_id: str, manager):
        self.chat_id = chat_id
        self.manager = manager
        self._gaze_timeout_task: asyncio.Task = None

    async def on_reply_start(self):
        await self.manager.controller.send_live2d_event("state", "start_thinking")

    async def on_start_replying(self):
        """Bot reply text is ready, now speaking/sending. Cancel timeout."""
        self._cancel_gaze_timeout()
        await self.manager.controller.send_live2d_event("state", "start_replying")

    async def on_reply_finished(self):
        """Reply fully done (audio finished or danmu sent). Reset gaze."""
        self._cancel_gaze_timeout()
        await self.manager.controller.send_live2d_event("state", "finish_reply")

    async def on_message_received(self):
        # Look at chat
        if self.manager and self.manager.controller:
            await self.manager.controller.send_live2d_event("state", "start_viewing")

        # Simulate thinking after a short delay
        await asyncio.sleep(5.0)
        if self.manager and self.manager.controller:
            await self.manager.controller.send_live2d_event("state", "start_thinking")

        # Auto-reset if no reply comes within 15 seconds (e.g., no_reply)
        self._schedule_gaze_timeout(15.0)

    def _schedule_gaze_timeout(self, timeout_seconds: float):
        self._cancel_gaze_timeout()
        self._gaze_timeout_task = asyncio.create_task(
            self._gaze_timeout(timeout_seconds)
        )

    def _cancel_gaze_timeout(self):
        if self._gaze_timeout_task and not self._gaze_timeout_task.done():
            self._gaze_timeout_task.cancel()
            self._gaze_timeout_task = None

    async def _gaze_timeout(self, timeout_seconds: float):
        try:
            await asyncio.sleep(timeout_seconds)
            if self.manager and self.manager.controller:
                self.manager.controller.logger.debug(
                    f"[AutoGaze] Timeout ({timeout_seconds}s) - auto-resetting gaze"
                )
                await self.manager.controller.send_live2d_event("state", "finish_reply")
        except asyncio.CancelledError:
            pass


class WatchingManager:
    def __init__(self, controller):
        self.controller = controller
        self.logger = controller.logger
        self.watching_list = []

    def get_watching_by_chat_id(self, chat_id: str) -> ChatWatching:
        for watching in self.watching_list:
            if watching.chat_id == chat_id:
                return watching
        new_watching = ChatWatching(chat_id, self)
        self.watching_list.append(new_watching)
        return new_watching

    async def on_message_received(self, chat_id="live_room"):
        await self.get_watching_by_chat_id(chat_id).on_message_received()

    async def on_reply_start(self, chat_id="live_room"):
        await self.get_watching_by_chat_id(chat_id).on_reply_start()

    async def on_start_replying(self, chat_id="live_room"):
        await self.get_watching_by_chat_id(chat_id).on_start_replying()

    async def on_reply_finished(self, chat_id="live_room"):
        await self.get_watching_by_chat_id(chat_id).on_reply_finished()
