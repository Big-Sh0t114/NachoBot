from maim_message import (
    MessageServer,
    Router,
    RouteConfig,
    TargetConfig,
    MessageBase,
    Seg,
)
from src.config import Config
from src.logger import logger
from src.plugins.base_tts_model import BaseTTSModel
from src.utils.audio_encode import encode_audio, encode_audio_stream
from src.utils import post_process
import asyncio
from typing import List, Tuple, Dict, Optional
import importlib
import toml
import random
from pathlib import Path


class TTSPipeline:
    tts_list: List[BaseTTSModel] = []

    def __init__(self, config_path: str):  # sourcery skip: dict-comprehension
        self.config: Config = Config(config_path)
        self.server = MessageServer(
            host=self.config.server.host,
            port=self.config.server.port,
        )

        # 设置路由
        route_config = {}
        for platform, url in self.config.routes.items():
            route_config[platform] = TargetConfig(url=url, token=None)

        self.router = Router(RouteConfig(route_config))

        self.server.register_message_handler(self.server_handle)
        self.router.register_class_handler(self.client_handle)

        # 按群/用户分组的文本缓冲队列和处理任务
        self.text_buffer_dict: Dict[str, asyncio.Queue[Tuple[str, MessageBase]]] = {}
        self.buffer_task_dict: Dict[str, asyncio.Task] = {}

    def import_module(self):
        """动态导入TTS适配"""
        for tts in self.config.enabled_plugin.enabled:
            # 动态导入模块
            module_name = f"src.plugins.{tts}"
            try:
                module = importlib.import_module(module_name)
                tts_class: BaseTTSModel = module.TTSModel()
                self.tts_list.append(tts_class)
            except ImportError as e:
                logger.error(f"Error importing {module_name}: {e}")
                raise
            except AttributeError as e:
                logger.error(f"Error accessing TTSModel in {module_name}: {e}")
                raise
            except Exception as e:
                logger.error(f"Unexpected error importing {module_name}: {e}")
                raise

    async def start(self):
        """启动服务器和路由，并导入设定的模块"""
        self.import_module()
        py_project_path = Path(__file__).parent / "pyproject.toml"
        toml_data = toml.load(py_project_path)
        logger.info(f"版本信息\n\n当前版本: {toml_data['project']['version']}\n")
        # 创建任务而不是直接返回 gather 结果
        self.server_task = asyncio.create_task(self.server.run())
        self.router_task = asyncio.create_task(self.router.run())
        # 返回任务以便外部可以等待或取消
        return self.server_task, self.router_task

    async def server_handle(self, message_data: dict):
        """处理服务器收到的消息"""
        message = MessageBase.from_dict(message_data)
        if message.message_info.format_info and "voice" in message.message_info.format_info.accept_format:
            message.message_info.format_info.accept_format.append("tts_text")
        await self.router.send_message(message)

    def process_seg(self, seg: Seg) -> Tuple[str, Optional[str]]:
        """处理消息段，提取文本内容与显式语种"""
        message_text = ""
        text_lang: Optional[str] = None
        if seg.type == "seglist":
            for s in seg.data:
                child_text, child_lang = self.process_seg(s)
                message_text += child_text
                if child_lang:
                    text_lang = child_lang
        if seg.type == "tts_text":
            if isinstance(seg.data, dict):
                message_text += seg.data.get("text", "")
                text_lang = seg.data.get("lang")
            else:
                message_text += seg.data
        return message_text, text_lang


    async def client_handle(self, message_dict: dict) -> None:
        # sourcery skip: remove-redundant-if
        """处理客户端收到的消息并进行TTS转换（分群缓冲）"""
        message = MessageBase.from_dict(message_dict)
        stream_mode = self.config.tts_base_config.stream_mode
        if message.message_segment.type != 'tts_text' and random.random() > self.config.probability.voice_probability:
            #  如果概率不满足，直接透传消息
            await self.server.send_message(message)
            return
        
        if stream_mode:
            await self.send_voice_stream(message)
            return

        message_text, text_lang = self.process_seg(message.message_segment)
        if not text_lang and message.message_info.additional_config:
            text_lang = message.message_info.additional_config.get("tts_language") or message.message_info.additional_config.get("text_lang")
        if message_text == "":
            # 非文本消息直接透传
            await self.server.send_message(message)
            return

        if not message_text:
            logger.warning("处理文本为空，跳过发送")
            return

        # 获取分组ID（优先群id，否则用户id）
        group_id = getattr(message.message_info.group_info, "group_id", None)
        if group_id is None:
            logger.warning("没有群消息id使用用户id代替")
            group_id = getattr(message.message_info.user_info, "user_id", None)
        if not group_id:
            logger.warning("无法定位目标发送位置，跳过TTS处理")
            await self.server.send_message(message)
            return
        group_id = str(group_id)

        # 保证队列存在
        if group_id not in self.text_buffer_dict:
            self.text_buffer_dict[group_id] = asyncio.Queue()
        # 创建处理任务
        if group_id not in self.buffer_task_dict:
            self.buffer_task_dict[group_id] = asyncio.create_task(self._buffer_queue_handler(group_id))
        # 将文本加入队列
        await self.text_buffer_dict[group_id].put((message_text, message))

    async def _buffer_queue_handler(self, group_id: str) -> None:
        """处理每个群/用户的缓冲队列，合成语音并发送"""
        message_text, latest_message_obj = await self.text_buffer_dict[group_id].get()
        self.text_buffer_dict[group_id].task_done()
        if not message_text or not latest_message_obj:
            logger.warning("数据为空，跳过处理")
            await self.cleanup_task(group_id)
            return
        text: str = message_text.strip()
        logger.info(f"[聊天: {group_id}]将合成文本: {text}")
        message = latest_message_obj
        text_lang = None
        if message.message_info.additional_config:
            text_lang = message.message_info.additional_config.get("tts_language") or message.message_info.additional_config.get("text_lang")
        new_seg = await self.get_voice_no_stream(text, message.message_info.platform, text_lang=text_lang)
        if not new_seg:
            logger.warning("语音消息为空，跳过发送")
            await self.cleanup_task(group_id)
            return
        message.message_segment = new_seg
        message.message_info.format_info.content_format = ["voice"]
        if not message.message_info.additional_config:
            message.message_info.additional_config = {}
        message.message_info.additional_config["original_text"] = text
        if text_lang:
            message.message_info.additional_config["text_lang"] = text_lang
            message.message_info.additional_config["tts_language"] = text_lang
        await self.server.send_message(message)
        await self.cleanup_task(group_id)
        return

    async def cleanup_task(self, group_id: str):
        task = self.buffer_task_dict.pop(group_id)
        task.cancel()
 
    async def get_voice_no_stream(self, text: str, platform: str, text_lang: str | None = None) -> Seg | None:
        """获取语音消息段"""
        if not self.tts_list:
            logger.warning("没有启用任何tts，跳过处理")
            return None
        # tts_class = random.choice(self.tts_list)
        tts_class = self.tts_list[0]
        try:
            # 使用非流式TTS
            audio_data = await tts_class.tts(text=text, platform=platform, text_lang=text_lang)
            if self.config.tts_base_config.post_process:
                # 如果启用了后处理，进行电话语音模拟
                audio_data = post_process.simulate_telephone_voice(audio_data)
            # 对整个音频数据进行base64编码
            encoded_audio = encode_audio(audio_data)
            # 创建语音消息
            return Seg(type="voice", data=encoded_audio)
        except Exception as e:
            logger.error(f"TTS处理过程中发生错误: {str(e)}")
            logger.info(f"文本为: {text}")
            return None

    async def send_voice_stream(self, message: MessageBase) -> None:
        """流式发送语音消息"""
        platform = message.message_info.platform
        message_text, text_lang = self.process_seg(message.message_segment)
        if not text_lang and message.message_info.additional_config:
            text_lang = message.message_info.additional_config.get("tts_language") or message.message_info.additional_config.get("text_lang")
        if not message_text:
            logger.warning("处理文本为空，跳过发送")
            return
        text = message_text
        if not self.tts_list:
            logger.warning("没有启用任何tts，跳过处理")
            return None
        # tts_class = random.choice(self.tts_list)
        tts_class = self.tts_list[0]
        try:
            audio_stream = await tts_class.tts_stream(text=text, platform=platform, text_lang=text_lang)
            # 从音频流中读取和处理数据
            for chunk in audio_stream:
                if chunk:  # 确保chunk不为空
                    try:
                        # 对音频数据进行base64编码
                        encoded_chunk = encode_audio_stream(chunk)
                        # 创建语音消息
                        new_seg = Seg(type="voice_stream", data=encoded_chunk)
                        message.message_segment = new_seg
                        message.message_info.format_info.content_format = ["voice_stream"]
                        if not message.message_info.additional_config:
                            message.message_info.additional_config = {}
                        message.message_info.additional_config["original_text"] = text
                        if text_lang:
                            message.message_info.additional_config["text_lang"] = text_lang

                        # 发送到下游
                        await self.server.send_message(message)
                    except Exception as e:
                        logger.error(f"处理音频块时发生错误: {str(e)}")
                        continue
            logger.info("流式语音消息发送完成")
        except Exception as e:
            logger.error(f"TTS处理过程中发生错误: {str(e)}")
            logger.info(f"文本为: {text}")
            return None

    async def stop(self):
        """停止服务器和路由"""
        logger.info("正在停止TTS服务...")
        # 停止所有正在运行的缓冲任务
        for _, task in list(self.buffer_task_dict.items()):
            if not task.done() and not task.cancelled():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"取消缓冲任务时出错: {e}")

        # 如果有任务属性，先取消这些任务
        tasks_to_cancel = []
        if hasattr(self, "server_task") and not self.server_task.done():
            self.server_task.cancel()
            tasks_to_cancel.append(self.server_task)

        if hasattr(self, "router_task") and not self.router_task.done():
            self.router_task.cancel()
            tasks_to_cancel.append(self.router_task)

        # 等待任务取消完成
        if tasks_to_cancel:
            try:
                await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
            except Exception as e:
                logger.error(f"等待任务取消时出错: {e}")

        # 安全地停止路由器（先停止路由器，因为它包含客户端连接）
        try:
            await self.router.stop()
        except Exception as e:
            logger.error(f"停止路由器时发生错误: {e}")

        # 安全地停止服务器
        try:
            await self.server.stop()
        except Exception as e:
            logger.error(f"停止服务器时发生错误: {e}")

        # 给一点时间让连接完全关闭
        await asyncio.sleep(0.1)

        logger.info("TTS服务已停止")


async def main():
    """主程序入口"""
    config_path = Path(__file__).parent / "configs" / "base.toml"
    pipeline = TTSPipeline(str(config_path))

    try:
        logger.info("正在启动TTS服务...")
        # 启动服务
        server_task, router_task = await pipeline.start()
        logger.info("TTS服务已启动，按 Ctrl+C 退出")

        # 等待任务完成或中断
        await asyncio.gather(server_task, router_task)

    except KeyboardInterrupt:
        logger.debug("\n接收到键盘中断信号...")
    except Exception as e:
        logger.error(f"运行过程中发生错误: {str(e)}")
    finally:
        logger.info("正在关闭服务...")
        try:
            # 增加超时时间，确保有足够时间清理资源
            await asyncio.wait_for(pipeline.stop(), timeout=15.0)
            logger.info("服务已安全关闭")
        except asyncio.TimeoutError:
            logger.warning("关闭服务超时，强制退出")
        except Exception as e:
            logger.error(f"关闭服务时发生错误: {str(e)}")

        # 额外的清理步骤：等待一小段时间让所有资源完全释放
        await asyncio.sleep(0.2)


if __name__ == "__main__":
    try:
        # 使用 asyncio.run() 来运行程序，这是现代化的做法
        # 它会自动处理事件循环的创建和清理
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n程序已退出")
    except Exception as e:
        logger.error(f"程序启动失败: {str(e)}")
    finally:
        # 给系统一点时间完成所有清理工作
        import time

        time.sleep(0.1)
