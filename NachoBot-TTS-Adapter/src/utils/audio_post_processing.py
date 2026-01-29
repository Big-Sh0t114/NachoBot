from pydub import AudioSegment
from pydub.generators import WhiteNoise
import traceback
import random
import io

"""
这部分代码我是一点也没测试
单纯只是拆分了可乐写的代码
pydub的音频处理功能我也不熟悉
而且pydub的类型注解也是稀烂
所以如果你启用了但是被炸飞了
请不要怪我
"""


def process_audio(
    audio_data: bytes,
    volume_reduction_db: float = 0,
    blow_up: bool = False,
    noise_level: float = 0,
) -> bytes:
    """
    对音频数据进行后处理（降低音量、添加杂音、柔化声音）

    Args:
        audio_data: 原始音频数据
        volume_reduction_db: 降低的音量（dB）
        blow_up: 是否为爆音模式
        noise_level: 噪声强度（0-1）

    Returns:
        处理后的音频数据
    """

    try:
        print("开始处理音频数据...")
        audio_segment = AudioSegment.from_wav(io.BytesIO(audio_data))
        original_duration = len(audio_segment)
        print(f"原始音频长度: {original_duration}ms")

        # 降低音量
        if volume_reduction_db > 0:
            audio_segment = decrease_volume(audio_segment, volume_reduction_db)

        # 柔化声音 - 使用低通滤波保留低频，降低高频尖锐感，实现声音钝化和柔化
        audio_segment = low_pass_filter(audio_segment, cutoff_frequency=2500)

        # 添加杂音 - 使用更加温和、自然的噪声
        if noise_level > 0 or blow_up:
            audio_segment = add_noise(audio_segment, noise_level, original_duration, blow_up)

        # 轻微混响效果
        audio_segment = add_reverb(audio_segment, original_duration, blow_up)

        # 转换回字节数据
        output = io.BytesIO()
        audio_segment.export(output, format="wav")
        processed_data = output.getvalue()
        print(f"音频处理完成，处理后大小: {len(processed_data)} 字节")
        return processed_data

    except Exception as e:
        print(f"音频后处理过程中发生错误: {str(e)}")
        print(f"详细错误信息: {traceback.format_exc()}")
        # 出错时返回原始音频
        return audio_data


def decrease_volume(audio_segment, volume_reduction_db: float) -> bytes:
    """
    降低音频音量

    Args:
        audio_data: 原始音频数据
        volume_reduction_db: 降低的音量（dB）

    Returns:
        处理后的音频数据
    """
    try:
        audio_segment = audio_segment - volume_reduction_db
        print(f"已降低音量 {volume_reduction_db}dB")
    except Exception as e:
        print(f"降低音量失败: {str(e)}，返回原始音频")
    return audio_segment


def low_pass_filter(audio_segment, cutoff_frequency: int) -> AudioSegment:
    """
    对音频应用低通滤波

    Args:
        audio_segment: 要处理的音频段
        cutoff_frequency: 截止频率（Hz），低于此频率的声音将被保留

    Returns:
        处理后的音频段
    """
    try:
        filtered_audio = audio_segment.low_pass_filter(cutoff_frequency)
        print(f"已应用低通滤波，截止频率为 {cutoff_frequency}Hz")
        return filtered_audio
    except Exception as e:
        print(f"应用低通滤波失败: {str(e)}，返回原始音频")
        return audio_segment


def add_noise(audio_segment, noise_level: float, original_duration: int, blow_up: bool) -> AudioSegment:
    """
    添加背景噪声

    Args:
        audio_segment: 要处理的音频段
        noise_level: 噪声强度（0-1）

    Returns:
        处理后的音频段
    """
    try:
        # 使用WhiteNoise并应用低通滤波使其接近粉红噪声效果
        # 生成噪声
        noise = WhiteNoise().to_audio_segment(duration=original_duration)
        print("已生成基础噪声")

        # 应用强烈的低通滤波使白噪声听起来更接近自然环境噪声
        if not blow_up:  # 只在非爆炸模式下应用滤波
            noise = noise.low_pass_filter(300)
            # 更强的滤波，只保留非常低的频率
            print("已对噪声应用低通滤波")
        else:
            print("爆炸模式：不对噪声应用滤波，保留原始白噪声的刺耳特性")

        # 调整噪声音量
        if blow_up:
            # 噪音拉满 - 使用最大噪声强度
            actual_noise_level = random.uniform(0.5, 2)
            # 提高噪声基准，使其接近原音频音量
            noise = noise - 10 * (actual_noise_level)
            audio_segment = audio_segment.overlay(noise)
            print(f"爆音模式！噪声强度设置为{actual_noise_level * 100:.1f}%")
        else:
            # 正常噪声处理
            noise = noise - (20 - 8 * noise_level)  # 噪声基准比原音频低较多
            audio_segment = audio_segment.overlay(noise)
            print(f"已添加{noise_level * 100:.1f}%强度的背景噪声")
    except Exception as e:
        print(f"添加噪声失败: {str(e)}")
    return audio_segment


def add_reverb(audio_segment, original_duration: int, blow_up: bool) -> AudioSegment:
    """
    添加混响效果，通过制作一个非常轻微的延迟副本并叠加实现

    Args:
        audio_segment: 要处理的音频段
        original_duration: 原始音频长度
        blow_up: 是否为爆音模式

    Returns:
        处理后的音频段
    """
    try:
        if original_duration > 100 and not blow_up:  # 爆音模式下不添加混响
            reverb_segment = audio_segment[10:] - 12  # 使用略延迟的副本，降低12dB
            audio_segment = audio_segment.overlay(reverb_segment, position=10)
            print("已添加轻微混响效果")
        elif blow_up and original_duration > 50:
            # 爆音模式下添加更强烈的混响
            reverb_segment = audio_segment[5:] - 6  # 更短的延迟，更高的音量
            audio_segment = audio_segment.overlay(reverb_segment, position=5)
            print("已添加强烈混响效果")
    except Exception as e:
        print(f"添加混响效果失败: {str(e)}")
    return audio_segment
