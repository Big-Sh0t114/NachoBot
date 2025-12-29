from maim_message import Seg, MessageBase
from typing import List, Dict

from src.logger import logger
from src.config import global_config
from src.utils import get_image_format, convert_image_to_gif


class SendMessageHandleClass:
    @classmethod
    def parse_seg_to_nc_format(cls, message_segment: Seg):
        parsed_payload: List = cls.process_seg_recursive(message_segment)
        return parsed_payload

    @classmethod
    def process_seg_recursive(cls, seg_data: Seg, in_forward: bool = False) -> List:
        payload: List = []
        if seg_data.type == "seglist":
            if not seg_data.data:
                return []
            for seg in seg_data.data:
                payload = cls.process_message_by_type(seg, payload, in_forward)
        else:
            payload = cls.process_message_by_type(seg_data, payload, in_forward)
        return payload

    @classmethod
    def process_message_by_type(cls, seg: Seg, payload: List, in_forward: bool = False) -> List:
        # sourcery skip: for-append-to-extend, reintroduce-else, swap-if-else-branches, use-named-expression
        new_payload = payload
        if seg.type == "reply":
            target_id = seg.data
            if target_id == "notice":
                return payload
            new_payload = cls.build_payload(payload, cls.handle_reply_message(target_id), True)
        elif seg.type == "text":
            text = seg.data
            if not text:
                return payload
            new_payload = cls.build_payload(payload, cls.handle_text_message(text), False)
        elif seg.type == "face":
            face_id = seg.data
            new_payload = cls.build_payload(payload, cls.handle_native_face_message(face_id), False)
        elif seg.type == "image":
            image = seg.data
            new_payload = cls.build_payload(payload, cls.handle_image_message(image), False)
        elif seg.type == "emoji":
            emoji = seg.data
            new_payload = cls.build_payload(payload, cls.handle_emoji_message(emoji), False)
        elif seg.type == "voice":
            voice = seg.data
            new_payload = cls.build_payload(payload, cls.handle_voice_message(voice), False)
        elif seg.type == "voiceurl":
            voice_url = seg.data
            new_payload = cls.build_payload(payload, cls.handle_voiceurl_message(voice_url), False)
        elif seg.type == "music":
            song_id = seg.data
            new_payload = cls.build_payload(payload, cls.handle_music_message(song_id), False)
        elif seg.type == "videourl":
            video_url = seg.data
            new_payload = cls.build_payload(payload, cls.handle_videourl_message(video_url), False)
        elif seg.type == "file":
            file_path = seg.data
            new_payload = cls.build_payload(payload, cls.handle_file_message(file_path), False)
        elif seg.type == "imageurl":
            image_url = seg.data
            new_payload = cls.build_payload(payload, cls.handle_imageurl_message(image_url), False)
        elif seg.type == "video":
            video_path = seg.data
            new_payload = cls.build_payload(payload, cls.handle_video_message(video_path), False)
        elif seg.type == "forward" and not in_forward:
            forward_message_content: List[Dict] = seg.data
            new_payload: List[Dict] = [
                cls.handle_forward_message(MessageBase.from_dict(item)) for item in forward_message_content
            ]  # 转发消息不能和其他消息一起发送
        return new_payload

    @classmethod
    def handle_forward_message(cls, item: MessageBase) -> Dict:
        # sourcery skip: remove-unnecessary-else
        message_segment: Seg = item.message_segment
        if message_segment.type == "id":
            return {"type": "node", "data": {"id": message_segment.data}}
        else:
            user_info = item.message_info.user_info
            content = cls.process_seg_recursive(message_segment, True)
            return {
                "type": "node",
                "data": {"name": user_info.user_nickname or "QQ用户", "uin": user_info.user_id, "content": content},
            }

    @staticmethod
    def build_payload(payload: List, addon: dict, is_reply: bool = False) -> List:
        # sourcery skip: for-append-to-extend, merge-list-append, simplify-generator
        if is_reply:
            temp_list = []
            temp_list.append(addon)
            for i in payload:
                if i.get("type") == "reply":
                    logger.debug("检测到多个回复，使用最新的回复")
                    continue
                temp_list.append(i)
            return temp_list
        else:
            payload.append(addon)
            return payload

    @staticmethod
    def handle_reply_message(id: str) -> dict:
        """处理回复消息"""
        return {"type": "reply", "data": {"id": id}}

    @staticmethod
    def handle_text_message(message: str) -> dict:
        """处理文本消息"""
        return {"type": "text", "data": {"text": message}}

    @staticmethod
    def handle_native_face_message(face_id: int) -> dict:
        # sourcery skip: remove-unnecessary-cast
        """处理原生表情消息"""
        return {"type": "face", "data": {"id": int(face_id)}}

    @staticmethod
    def handle_image_message(encoded_image: str) -> dict:
        """处理图片消息"""
        return {
            "type": "image",
            "data": {
                "file": f"base64://{encoded_image}",
                "subtype": 0,
            },
        }  # base64 编码的图片

    @staticmethod
    def handle_emoji_message(encoded_emoji: str) -> dict:
        """处理表情消息"""
        encoded_image = encoded_emoji
        image_format = get_image_format(encoded_emoji)
        if image_format != "gif":
            encoded_image = convert_image_to_gif(encoded_emoji)
        return {
            "type": "image",
            "data": {
                "file": f"base64://{encoded_image}",
                "subtype": 1,
                "summary": "[动画表情]",
            },
        }

    @staticmethod
    def handle_voice_message(encoded_voice: str) -> dict:
        """处理语音消息"""
        if not global_config.voice.use_tts:
            logger.warning("未启用语音消息处理")
            return {}
        if not encoded_voice:
            return {}
        return {
            "type": "record",
            "data": {"file": f"base64://{encoded_voice}"},
        }

    @staticmethod
    def handle_voiceurl_message(voice_url: str) -> dict:
        """处理语音链接消息"""
        return {
            "type": "record",
            "data": {"file": voice_url},
        }

    @staticmethod
    def handle_music_message(song_id: str) -> dict:
        """处理音乐消息"""
        return {
            "type": "music",
            "data": {"type": "163", "id": song_id},
        }

    @staticmethod
    def handle_videourl_message(video_url: str) -> dict:
        """处理视频链接消息"""
        return {
            "type": "video",
            "data": {"file": video_url},
        }

    @staticmethod
    def handle_file_message(file_path: str) -> dict:
        """处理文件消息"""
        return {
            "type": "file",
            "data": {"file": f"file://{file_path}"},
        }

    @staticmethod
    def handle_imageurl_message(image_url: str) -> dict:
        """处理图片链接消息"""
        return {
            "type": "image",
            "data": {"file": image_url},
        }

    @staticmethod
    def handle_video_message(encoded_video: str) -> dict:
        """处理视频消息（base64格式）"""
        if not encoded_video:
            logger.error("视频数据为空")
            return {}
            
        logger.info(f"处理视频消息，数据长度: {len(encoded_video)} 字符")
        
        return {
            "type": "video",
            "data": {"file": f"base64://{encoded_video}"},
        }
