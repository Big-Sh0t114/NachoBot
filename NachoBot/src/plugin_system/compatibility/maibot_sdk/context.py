from typing import Any, List, Dict, Optional
from src.common.logger import get_logger

logger = get_logger("maibot_sdk.context")

class SendAPIWrapper:
    async def text(self, text: str, stream_id: str) -> bool:
        from src.plugin_system.apis import send_api
        try:
            return await send_api.text_to_stream(text=text, stream_id=stream_id)
        except Exception as e:
            logger.error(f"Error in SendAPIWrapper.text: {e}")
            return False

    async def forward(self, messages: List[Dict[str, Any]], stream_id: str) -> bool:
        from src.plugin_system.apis import send_api
        from src.common.data_models.message_data_model import ReplySetModel, ReplyContent, ReplyContentType, ForwardNode
        try:
            forward_nodes = []
            for msg in messages:
                user_id = str(msg.get("user_id") or "0")
                nickname = str(msg.get("nickname") or "anonymous")
                segments = msg.get("segments") or []
                
                node_contents = []
                for seg in segments:
                    seg_type = seg.get("type", "text")
                    seg_content = seg.get("content", "")
                    
                    if seg_type == "text":
                        node_contents.append(ReplyContent(content_type=ReplyContentType.TEXT, content=seg_content))
                    elif seg_type == "image":
                        node_contents.append(ReplyContent(content_type=ReplyContentType.IMAGE, content=seg_content))
                    elif seg_type == "emoji":
                        node_contents.append(ReplyContent(content_type=ReplyContentType.EMOJI, content=seg_content))
                        
                node = ForwardNode.construct_as_created_node(
                    user_id=user_id,
                    user_nickname=nickname,
                    content=node_contents
                )
                forward_nodes.append(node)
                
            reply_set = ReplySetModel()
            reply_set.add_forward_content(forward_nodes)
            return await send_api.custom_reply_set_to_stream(reply_set, stream_id)
        except Exception as e:
            logger.error(f"Error in SendAPIWrapper.forward: {e}")
            return False

    async def hybrid(self, segments: List[Dict[str, Any]], stream_id: str) -> bool:
        from src.plugin_system.apis import send_api
        from src.common.data_models.message_data_model import ReplySetModel, ReplyContent, ReplyContentType
        try:
            hybrid_items = []
            for seg in segments:
                seg_type = seg.get("type", "text")
                seg_content = seg.get("content", "")
                
                if seg_type == "text":
                    hybrid_items.append(ReplyContent(content_type=ReplyContentType.TEXT, content=seg_content))
                elif seg_type == "image":
                    hybrid_items.append(ReplyContent(content_type=ReplyContentType.IMAGE, content=seg_content))
                elif seg_type == "emoji":
                    hybrid_items.append(ReplyContent(content_type=ReplyContentType.EMOJI, content=seg_content))
                    
            reply_set = ReplySetModel()
            reply_set.add_hybrid_content(hybrid_items)
            return await send_api.custom_reply_set_to_stream(reply_set, stream_id)
        except Exception as e:
            logger.error(f"Error in SendAPIWrapper.hybrid: {e}")
            return False

class EmojiAPIWrapper:
    async def get_random(self, count: int) -> List[Dict[str, Any]]:
        from src.plugin_system.apis import emoji_api
        try:
            emojis = await emoji_api.get_random(count)
            results = []
            for base64_str, desc, emotion in emojis:
                results.append({
                    "base64": base64_str,
                    "description": desc,
                    "emotion": emotion
                })
            return results
        except Exception as e:
            logger.error(f"Error in EmojiAPIWrapper.get_random: {e}")
            return []

class PluginContext:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.send = SendAPIWrapper()
        self.emoji = EmojiAPIWrapper()

    @property
    def config(self) -> Any:
        return self.plugin.config
