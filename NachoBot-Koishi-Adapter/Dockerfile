FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /koishi-adapter

# 安装本地 ncnk_message 包
COPY ncnk_message /ncnk_message
RUN uv pip install --system -e /ncnk_message

# 复制项目文件并安装依赖
COPY pyproject.toml ./
RUN uv pip install --system --upgrade pip
RUN uv pip install --system .

# 复制项目代码
COPY . .

ENTRYPOINT ["python", "main.py"]
