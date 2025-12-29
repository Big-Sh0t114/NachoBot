FROM python:3.13.2-slim-bookworm

# 设置工作目录
WORKDIR /maimbot_tts_adapter

# 将当前目录下的文件复制到容器的工作目录中
COPY . .

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 创建配置目录并从模板复制默认配置
RUN mkdir -p /maimbot_tts_adapter/configs && \
    cp /maimbot_tts_adapter/template_configs/base_template.toml /maimbot_tts_adapter/configs/base.toml && \
    cp /maimbot_tts_adapter/template_configs/gpt-sovits_template.toml /maimbot_tts_adapter/configs/gpt-sovits.toml

# 暴露端口（如果需要的话，根据你的api_v2.py实际情况调整）
EXPOSE 8070

# 定义可以被映射的卷
VOLUME ["/maimbot_tts_adapter/configs"]

# 运行应用
CMD ["python", "main.py"]
