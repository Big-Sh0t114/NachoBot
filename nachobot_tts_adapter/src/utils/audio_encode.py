import io
import wave
import base64


def encode_audio(audio_data: bytes, media_type: str = "wav") -> str:
    """对音频数据进行base64编码

    Args:
        audio_data: 原始音频数据
        media_type: 音频格式，默认为wav

    Returns:
        base64编码后的音频数据
    """
    # 确保音频是wav格式
    if media_type != "wav":
        # 先将音频数据转换为wav格式
        with io.BytesIO(audio_data) as audio_io:
            with wave.open(audio_io, "rb") as wav_file:
                # 获取音频参数
                channels = wav_file.getnchannels()
                width = wav_file.getsampwidth()
                framerate = wav_file.getframerate()
                frames = wav_file.readframes(wav_file.getnframes())

                # 创建新的wav文件
                with io.BytesIO() as wav_io:
                    with wave.open(wav_io, "wb") as new_wav:
                        new_wav.setnchannels(channels)
                        new_wav.setsampwidth(width)
                        new_wav.setframerate(framerate)
                        new_wav.writeframes(frames)
                    audio_data = wav_io.getvalue()

    # base64编码
    return base64.b64encode(audio_data).decode("utf-8")


def encode_audio_stream(audio_chunk: bytes, media_type: str = "wav") -> str:
    """对音频数据块进行base64编码

    Args:
        audio_chunk: 音频数据块

    Returns:
        base64编码后的数据
    """
    return base64.b64encode(audio_chunk).decode("utf-8")
