import numpy as np 
import wave
from io import BytesIO
from scipy.signal import butter, lfilter

def simulate_telephone_voice(audio_bytes) -> bytes:
    """
    处理音频数据，添加电话语音效果（带通滤波、轻微失真和噪声）
    """
    # 创建类文件对象
    audio_io = BytesIO(audio_bytes)
    
    # 使用wave读取音频
    with wave.open(audio_io, 'rb') as wav:
        n_channels = wav.getnchannels()
        samp_width = wav.getsampwidth()
        frame_rate = wav.getframerate()
        n_frames = wav.getnframes()
        raw_data = wav.readframes(n_frames)

    # 将字节数据转换为numpy数组
    dtype_map = {1: np.int8, 2: np.int16, 4: np.int32}
    dtype = dtype_map.get(samp_width, np.int16)
    audio_array = np.frombuffer(raw_data, dtype=dtype)
    
    # 处理多声道数据（转换为单声道）
    if n_channels > 1:
        audio_array = audio_array.reshape(-1, n_channels)
        # 取平均值转换为单声道
        audio_array = np.mean(audio_array, axis=1)
    
    # 归一化到[-1, 1]范围
    max_val = np.max(np.abs(audio_array))
    if max_val > 0:
        audio_array = audio_array.astype(np.float32) / max_val

    # 1. 添加带通滤波器
    def butter_bandpass(lowcut, highcut, fs, order=5):
        nyq = 0.5 * fs
        low = lowcut / nyq
        high = highcut / nyq
        b, a = butter(order, [low, high], btype='band')
        return b, a
    
    def bandpass_filter(data, lowcut, highcut, fs, order=5):
        b, a = butter_bandpass(lowcut, highcut, fs, order=order)
        y = lfilter(b, a, data)
        return y

    # 应用频带滤波器
    filtered_audio = bandpass_filter(audio_array, 200, 5000, frame_rate)
    
    # 2. 添加轻微失真效果（软削波）
    def soft_clip(x, threshold=0.95):
        """
        软削波函数 - 模拟模拟设备的过载
        """
        # 线性区域
        idx = np.abs(x) <= threshold
        # 软削波区域
        y = np.zeros_like(x)
        y[idx] = x[idx]
        y[~idx] = np.sign(x[~idx]) * (threshold + (1 - threshold) * 
                                    np.tanh((np.abs(x[~idx]) - threshold) / (1 - threshold)))
        return y
    
    distorted_audio = soft_clip(filtered_audio * 1.05)  # 先增加增益再削波
    
    def add_ambient_noise(audio, noise_level=0.05):
        """添加环境噪声（低频嗡嗡声）"""
        t = np.arange(len(audio)) / frame_rate
        # 创建低频嗡嗡声（50Hz和120Hz）
        hum = 0.3 * np.sin(2 * np.pi * 50 * t) + 0.2 * np.sin(2 * np.pi * 120 * t)
        # 添加随机噪声
        noise = np.random.normal(0, noise_level, len(audio))
        return audio + hum * noise_level + noise * 0.7
    
    noisy_audio = add_ambient_noise(distorted_audio, noise_level=0.02)
    
    # 4. 添加轻微混响效果（简单的回声）
    def add_reverb(audio, delay=0.05, decay=0.25):
        """添加简单的混响效果"""
        delay_samples = int(delay * frame_rate)
        wet = np.zeros_like(audio)
        # 添加延迟信号
        wet[delay_samples:] = audio[:-delay_samples] * decay
        return audio * 0.7 + wet * 0.3
    
    processed_audio = add_reverb(noisy_audio, delay=0.04, decay=0.25)
    
    # 5. 添加轻微压缩（模拟低质量麦克风）
    def simple_compressor(audio, threshold=0.86, ratio=1.18):
        """简单压缩器效果"""
        # 找出超过阈值的部分
        above_threshold = np.abs(audio) > threshold
        # 压缩超过阈值的部分
        compressed = np.copy(audio)
        compressed[above_threshold] = np.sign(audio[above_threshold]) * (
            threshold + (np.abs(audio[above_threshold]) - threshold) / ratio
        )
        return compressed
    
    compressed_audio = simple_compressor(processed_audio)
    
    # 6. 最后归一化防止削波
    max_val = np.max(np.abs(compressed_audio))
    if max_val > 0:
        compressed_audio = compressed_audio / max_val
    
    # 将处理后的音频转换回原始格式
    # 根据原始采样宽度转换
    if samp_width == 1:  # 8-bit
        processed_int = (compressed_audio * 127).astype(np.int8)
    elif samp_width == 2:  # 16-bit
        processed_int = (compressed_audio * 32767).astype(np.int16)
    elif samp_width == 4:  # 32-bit
        processed_int = (compressed_audio * 2147483647).astype(np.int32)
    else:
        # 默认使用16-bit
        processed_int = (compressed_audio * 32767).astype(np.int16)
    
    # 将处理后的数据写回BytesIO
    output_io = BytesIO()
    with wave.open(output_io, 'wb') as wav_out:
        wav_out.setnchannels(1)  # 输出单声道
        wav_out.setsampwidth(samp_width)
        wav_out.setframerate(frame_rate)
        wav_out.writeframes(processed_int.tobytes())
    
    # 获取处理后的bytes
    processed_bytes = output_io.getvalue()
    return processed_bytes