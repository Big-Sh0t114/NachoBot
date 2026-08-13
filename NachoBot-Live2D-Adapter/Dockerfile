FROM python:3.13-windowsservercore-ltsc2022

WORKDIR C:/workspace/NachoBot-Live2D-Adapter

# live2d-py ships a Windows-only native wheel, so this image deliberately
# targets the Windows container engine rather than a Linux base image.
RUN python -m pip install --no-cache-dir --upgrade pip uv

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .

EXPOSE 8766

ENTRYPOINT ["C:\\workspace\\NachoBot-Live2D-Adapter\\.venv\\Scripts\\python.exe", "-m", "live2d_adapter", "--config", "config.toml"]
