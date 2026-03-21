FROM python:3.13-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /bilibili-adapter

# 系统依赖：build-essential（stringzilla 编译）、xvfb（屏幕监控）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖列表并安装
COPY requirements.txt pyproject.toml ./
RUN uv pip install --system --upgrade pip
RUN uv pip install --system -r requirements.txt

# 复制项目代码
COPY . .

ENTRYPOINT ["python", "main.py"]
