FROM python:3.13.5-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 工作目录
WORKDIR /NachoBot

# 复制依赖列表
COPY requirements.txt .
# 同级目录下需要有 ncnk_message NachoBot-LPMM
#COPY ncnk_message /ncnk_message
COPY NachoBot-LPMM /NachoBot-LPMM

# 编译器
RUN apt-get update && apt-get install -y build-essential

# lpmm编译安装
RUN cd /NachoBot-LPMM && uv pip install --system -r requirements.txt
RUN uv pip install --system Cython py-cpuinfo setuptools
RUN cd /NachoBot-LPMM/lib/quick_algo && python build_lib.py --cleanup --cythonize --install


# 安装依赖
RUN uv pip install --system --upgrade pip
#RUN uv pip install --system -e /ncnk_message
RUN uv pip install --system -r requirements.txt

# 复制项目代码
COPY . .

EXPOSE 8000

ENTRYPOINT [ "python","bot.py" ]