import asyncio
import importlib
import logging
import os
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import toml
from fastapi import HTTPException, Response
from pydantic import BaseModel, Field

_NACHOBOT_PATH = Path(__file__).resolve().parents[1] / "NachoBot"
if (_NACHOBOT_PATH / "ncnk_message").is_dir():
    nachobot_path = str(_NACHOBOT_PATH)
    if nachobot_path not in sys.path:
        sys.path.insert(1, nachobot_path)

from ncnk_message import (  # noqa: E402
    MessageServer,
    Router,
    RouteConfig,
    TargetConfig,
    MessageBase,
    Seg,
    FormatInfo,
    get_core_token_from_env,
)

# Keep all Hugging Face models in the adapter-owned cache directory.
# Honour both the NachoBot-specific endpoint and an existing standard
# HF_ENDPOINT instead of forcing the official Hub. launchbot.bat defaults
# NACHOBOT_HF_ENDPOINT to hf-mirror.com for mainland-China deployments.
os.environ["HF_HOME"] = str(Path(__file__).parent / "models" / "hf_cache")
if os.getenv("NACHOBOT_HF_ENDPOINT", "").strip():
    os.environ["HF_ENDPOINT"] = os.environ["NACHOBOT_HF_ENDPOINT"].strip()
else:
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "10")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")

# 隐藏 ncnk_message 的冗余日志
logging.getLogger("ncnk_message").setLevel(logging.CRITICAL)

from nachobot_multimodal.config import Config  # noqa: E402
from nachobot_multimodal.logger import logger  # noqa: E402
from nachobot_multimodal.tts.base import BaseTTSModel  # noqa: E402
from nachobot_multimodal.utils.audio_encode import encode_audio, encode_audio_stream  # noqa: E402
from nachobot_multimodal.utils import post_process  # noqa: E402


class WebUITTSRequest(BaseModel):
    """Direct synthesis request from the local WebUI."""

    text: str = Field(min_length=1, max_length=10_000)
    platform: str = Field(default="webui", max_length=64)
    text_lang: str | None = Field(default=None, max_length=32)


class TTSPipeline:

    def __init__(
        self,
        config_path: str,
        no_local_models: bool | None = None,
    ):  # sourcery skip: dict-comprehension
        self.tts_list: List[BaseTTSModel] = []
        self._emotion_classifier = None
        self._emotion_config = None
        self.no_local_models = (
            no_local_models
            if no_local_models is not None
            else os.environ.get("NACHOBOT_NO_LOCAL_MODELS") == "1"
        )
        self.config: Config = Config(config_path)
        # 根据配置刷新日志级别
        from nachobot_multimodal.logger import set_logging_level

        set_logging_level(self.config.config_data["debug"].get("logging_level", "INFO"))
        self.server = MessageServer(
            host=self.config.server.host,
            port=self.config.server.port,
        )

        # 设置路由
        route_config = {}
        for platform, url in self.config.routes.items():
            route_config[platform] = TargetConfig(
                url=url,
                token=get_core_token_from_env(),
            )

        self.router = Router(RouteConfig(route_config))

        self.server.register_message_handler(self.server_handle)
        self.router.register_class_handler(self.client_handle)

        # 按群/用户分组的文本缓冲队列和处理任务
        self.text_buffer_dict: Dict[str, asyncio.Queue[Tuple[str, MessageBase]]] = {}
        self.buffer_task_dict: Dict[str, asyncio.Task] = {}
        self._webui_tts_lock = asyncio.Lock()

    def import_module(self):
        """动态导入TTS适配"""
        if self.no_local_models:
            logger.info("无模型模式已启用，跳过 TTS 和情感分类模型加载")
            return

        enabled = self.config.enabled_plugin.enabled
        # 互斥校验：GPT_Sovits 和 Vox 不可同时启用
        if "GPT_Sovits" in enabled and "Vox" in enabled:
            raise ValueError(
                "GPT_Sovits 和 Vox 不可同时启用，请在 base.toml 的 [enabled_tts] 中只选择其一"
            )
        for tts in enabled:
            # 动态导入模块
            module_name = f"nachobot_multimodal.tts.backends.{tts}"
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

        # 情感分类器：遍历已加载的 TTS 模型，找到第一个带有 emotion 配置的模型
        # （不再依赖 enabled 列表和 tts_list 索引，避免从 GPT_Sovits 切回 Vox 时
        #   因类变量残留导致情感系统未初始化的问题）
        for tts_model in self.tts_list:
            emotion_cfg = getattr(getattr(tts_model, 'config', None), 'emotion', None)
            if emotion_cfg and getattr(emotion_cfg, 'enabled', False):
                try:
                    from nachobot_multimodal.utils.emotion_classifier import EmotionClassifier
                    self._emotion_config = emotion_cfg
                    self._emotion_classifier = EmotionClassifier(
                        model_name=emotion_cfg.classifier_model,
                        device=emotion_cfg.classifier_device,
                        use_fp16=emotion_cfg.use_fp16,
                    )
                    logger.info("情感分类系统已在 TTS Adapter 服务端启用（模型将在首次使用时加载）")
                except Exception as e:
                    logger.warning(f"情感分类器初始化失败，将使用默认预设: {e}")
                break

    async def start(self):
        """启动服务器和路由，并导入设定的模块"""
        self.import_module()
        self._register_http_endpoints()
        py_project_path = Path(__file__).parent / "pyproject.toml"
        toml_data = toml.load(py_project_path)
        logger.info(f"版本信息\n\n当前版本: {toml_data['project']['version']}\n")
        # 创建任务而不是直接返回 gather 结果
        self.server_task = asyncio.create_task(self.server.run())
        self.router_task = asyncio.create_task(self.router.run())
        # 返回任务以便外部可以等待或取消
        return self.server_task, self.router_task

    def _register_http_endpoints(self):
        """在 WebSocket 服务器的 FastAPI 应用上注册 HTTP 接口"""
        app = self.server.connection.app

        @app.get("/api/emotion_preset")
        async def emotion_preset_endpoint(text: str):
            """情感预设解析接口

            供外部适配器（DiscordVC/Bilibili/UniversalVC）远程调用，
            无需在适配器侧安装 torch/transformers。

            Returns:
                {"preset_name": "..."}  — 如果分类器可用且匹配到预设
                {"preset_name": null}   — 如果分类器不可用或无匹配
            """
            preset_name = self._resolve_emotion_preset(text)
            return {"preset_name": preset_name}

        @app.get("/api/health")
        async def health_endpoint():
            return {
                "status": "ok",
                "mode": "relay_only" if self.no_local_models else "tts",
                "emotion_classifier": self._emotion_classifier is not None,
                "tts_backends": [type(t).__module__ for t in self.tts_list],
            }

        @app.post("/api/tts")
        async def webui_tts_endpoint(body: WebUITTSRequest):
            """Generate one complete WAV response for the local WebUI."""
            text = body.text.strip()
            if not text:
                raise HTTPException(status_code=400, detail="TTS 文本不能为空")
            if not self.tts_list:
                raise HTTPException(status_code=503, detail="没有启用任何 TTS 引擎")

            tts_model = self.tts_list[0]
            try:
                async with self._webui_tts_lock:
                    preset_name = self._resolve_emotion_preset(text)
                    audio_data = await tts_model.tts(
                        text=text,
                        platform=body.platform or "webui",
                        text_lang=body.text_lang,
                        preset_name=preset_name,
                    )
            except Exception as exc:
                logger.exception("WebUI TTS 生成失败")
                raise HTTPException(status_code=502, detail=f"TTS 生成失败：{exc}") from exc

            if not audio_data:
                raise HTTPException(status_code=502, detail="TTS 服务返回了空音频")
            return Response(
                content=audio_data,
                media_type="audio/wav",
                headers={"Cache-Control": "no-store"},
            )

        if self.no_local_models:
            logger.info(
                "已注册 HTTP 兼容接口（无模型模式：TTS 和情感分类均禁用）: "
                "/api/emotion_preset, /api/health, /api/tts"
            )
        else:
            logger.info("已注册 HTTP 接口: /api/emotion_preset, /api/health, /api/tts")

    async def server_handle(self, message_data: dict):
        """处理服务器收到的消息"""
        message = MessageBase.from_dict(message_data)
        if (
            not self.no_local_models
            and message.message_info.format_info
            and "voice" in message.message_info.format_info.accept_format
        ):
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
        if self.no_local_models:
            await self.server.send_message(message)
            return

        stream_mode = self.config.tts_base_config.stream_mode
        if message.message_segment.type != "tts_text" and random.random() > self.config.probability.voice_probability:
            #  如果概率不满足，直接透传消息
            await self.server.send_message(message)
            return

        if stream_mode:
            await self.send_voice_stream(message)
            return

        message_text, text_lang = self.process_seg(message.message_segment)
        if not text_lang and message.message_info.additional_config:
            text_lang = message.message_info.additional_config.get(
                "tts_language"
            ) or message.message_info.additional_config.get("text_lang")
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
        try:
            while not self.text_buffer_dict[group_id].empty():
                message_text, latest_message_obj = await self.text_buffer_dict[group_id].get()
                try:
                    if not message_text or not latest_message_obj:
                        logger.warning("数据为空，跳过处理")
                        continue
                    text: str = message_text.strip()
                    logger.info(f"[聊天: {group_id}]将合成文本: {text}")
                    message = latest_message_obj
                    text_lang = None
                    if message.message_info.additional_config:
                        text_lang = message.message_info.additional_config.get(
                            "tts_language"
                        ) or message.message_info.additional_config.get("text_lang")
                    new_seg = await self.get_voice_no_stream(text, message.message_info.platform, text_lang=text_lang)
                    if not new_seg:
                        logger.warning("语音消息为空，跳过发送")
                        continue
                    if not message.message_info.format_info:
                        message.message_info.format_info = FormatInfo(
                            content_format=[],
                            accept_format=[],
                        )
                    message.message_segment = new_seg
                    message.message_info.format_info.content_format = ["voice"]
                    if not message.message_info.additional_config:
                        message.message_info.additional_config = {}
                    message.message_info.additional_config["original_text"] = text
                    if text_lang:
                        message.message_info.additional_config["text_lang"] = text_lang
                        message.message_info.additional_config["tts_language"] = text_lang
                    logger.debug(
                        f"TTS->Napcat 即将发送: platform={message.message_info.platform}, formats={message.message_info.format_info.content_format}"
                    )
                    ok = await self.server.send_message(message)
                    logger.info(
                        f"TTS->Napcat send: platform={message.message_info.platform}, ok={ok}, formats={message.message_info.format_info.content_format}"
                    )
                    if not ok:
                        logger.warning("send_message 返回 False，检查平台映射或连接状态")
                except Exception as exc:
                    logger.exception(f"TTS->Napcat 发送异常: {exc}")
                finally:
                    self.text_buffer_dict[group_id].task_done()
        finally:
            await self.cleanup_task(group_id)

    async def cleanup_task(self, group_id: str):
        task = self.buffer_task_dict.pop(group_id)
        task.cancel()

    def _resolve_emotion_preset(self, text: str) -> str | None:
        """通过情感分类确定使用的预设（仅在服务端运行）

        Returns:
            preset_name: 匹配的预设名称，或 None 表示使用平台默认
        """
        if not self._emotion_classifier or not self._emotion_config:
            return None

        emotion_cfg = self._emotion_config
        try:
            tag, confidence = self._emotion_classifier.classify(
                text, emotion_cfg.available_tags
            )

            if confidence < emotion_cfg.confidence_threshold:
                tag = emotion_cfg.default_emotion
                logger.info(
                    f"情感置信度不足 ({confidence:.3f} < {emotion_cfg.confidence_threshold})，"
                    f"回退默认情感: {tag}"
                )
            else:
                logger.info(f"情感分类结果: {tag} (置信度: {confidence:.3f})")

            # 查找标签对应的预设名
            preset_name = emotion_cfg.tag_preset_map.get(tag)
            if preset_name:
                logger.info(f"情感分类选择预设: {preset_name}")
                return preset_name
            else:
                logger.warning(f"情感 '{tag}' 无有效预设映射，使用平台默认预设")
                return None
        except Exception as e:
            logger.warning(f"情感分类异常，使用平台默认预设: {e}")
            return None

    async def get_voice_no_stream(self, text: str, platform: str, text_lang: str | None = None) -> Seg | None:
        """获取语音消息段"""
        if not self.tts_list:
            logger.warning("没有启用任何tts，跳过处理")
            return None
        # tts_class = random.choice(self.tts_list)
        tts_class = self.tts_list[0]
        try:
            # 服务端情感分类，确定预设名
            preset_name = self._resolve_emotion_preset(text)
            # 使用非流式TTS
            audio_data = await tts_class.tts(text=text, platform=platform, text_lang=text_lang, preset_name=preset_name)
            if not audio_data:
                logger.warning("TTS 返回空音频数据，跳过发送")
                return None
            if self.config.tts_base_config.post_process:
                # 如果启用了后处理，进行电话语音模拟
                audio_data = post_process.simulate_telephone_voice(audio_data)
            # 对整个音频数据进行base64编码
            encoded_audio = encode_audio(audio_data)
            logger.debug(f"生成语音数据长度: {len(encoded_audio)} (base64)")
            # 创建语音消息
            return Seg(type="voice", data=encoded_audio)
        except Exception as e:
            logger.error(f"TTS处理过程中发生错误: {str(e)}")
            logger.info(f"文本为: {text}")
            return None

    async def send_voice_stream(self, message: MessageBase) -> None:
        """流式发送语音消息 (真流式)"""
        platform = message.message_info.platform
        message_text, text_lang = self.process_seg(message.message_segment)
        if not text_lang and message.message_info.additional_config:
            text_lang = message.message_info.additional_config.get(
                "tts_language"
            ) or message.message_info.additional_config.get("text_lang")
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
            # 服务端情感分类，确定预设名
            preset_name = self._resolve_emotion_preset(text)
            # 修复 await async generator 的 Bug
            audio_stream = tts_class.tts_stream(text=text, platform=platform, text_lang=text_lang, preset_name=preset_name)
            async def handle_chunk(chunk):
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
                        try:
                            ok = await self.server.send_message(message)
                            logger.debug(f"流式分片发送: platform={message.message_info.platform}, ok={ok}")
                            if not ok:
                                logger.warning(f"流式语音发送失败 platform={message.message_info.platform}")
                        except Exception as exc:
                            logger.exception(f"流式发送异常: {exc}")
                    except Exception as e:
                        logger.error(f"处理音频块时发生错误: {str(e)}")

            # 从音频流中读取和处理数据 (兼容同步 and 异步迭代器)
            if hasattr(audio_stream, "__aiter__"):
                async for chunk in audio_stream:
                    await handle_chunk(chunk)
            else:
                for chunk in audio_stream:
                    await handle_chunk(chunk)
            
            logger.info("流式语音消息发送完成")
        except Exception as e:
            logger.error(f"TTS处理过程中发生错误: {str(e)}")
            logger.info(f"文本为: {text}")
            return None

    async def stop(self):
        """停止服务器和路由"""
        logger.info("正在停止{}...", "8070 无模型中继服务" if self.no_local_models else "TTS服务")
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

        logger.info("{}已停止", "8070 无模型中继服务" if self.no_local_models else "TTS服务")


async def main():
    """主程序入口"""
    config_path = Path(__file__).parent / "configs" / "base.toml"
    pipeline = TTSPipeline(
        str(config_path),
        no_local_models="--no-local-models" in sys.argv[1:],
    )

    try:
        logger.info(
            "正在启动{}...",
            "8070 无模型中继服务" if pipeline.no_local_models else "TTS服务",
        )
        # 启动服务
        server_task, router_task = await pipeline.start()
        logger.info(
            "{}已启动，按 Ctrl+C 退出",
            "8070 无模型中继服务" if pipeline.no_local_models else "TTS服务",
        )

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
