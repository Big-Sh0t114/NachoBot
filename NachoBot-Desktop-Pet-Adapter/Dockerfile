FROM python:3.13-windowsservercore-ltsc2022

WORKDIR C:/workspace

# live2d-py ships a Windows-only native wheel, so this image deliberately
# targets the Windows container engine rather than a Linux base image.
RUN python -m pip install --no-cache-dir --upgrade pip uv

COPY NachoBot-Desktop-Pet-Adapter/pyproject.toml ./NachoBot-Desktop-Pet-Adapter/pyproject.toml
COPY NachoBot-Desktop-Pet-Adapter/uv.lock ./NachoBot-Desktop-Pet-Adapter/uv.lock
COPY NachoBot-Desktop-Pet-Adapter/README.md ./NachoBot-Desktop-Pet-Adapter/README.md
COPY NachoBot-Desktop-Pet-Adapter/desktop_pet_adapter ./NachoBot-Desktop-Pet-Adapter/desktop_pet_adapter
COPY NachoBot-Desktop-Pet-Adapter/renderers/live2d/pyproject.toml ./NachoBot-Desktop-Pet-Adapter/renderers/live2d/pyproject.toml
COPY NachoBot-Desktop-Pet-Adapter/renderers/live2d/uv.lock ./NachoBot-Desktop-Pet-Adapter/renderers/live2d/uv.lock
COPY NachoBot-Live2D-Adapter/pyproject.toml ./NachoBot-Live2D-Adapter/pyproject.toml
COPY NachoBot-Live2D-Adapter/README.md ./NachoBot-Live2D-Adapter/README.md
COPY NachoBot-Live2D-Adapter/live2d_adapter ./NachoBot-Live2D-Adapter/live2d_adapter

WORKDIR C:/workspace/NachoBot-Desktop-Pet-Adapter
RUN uv sync --project renderers/live2d --locked --no-dev

EXPOSE 8766

ENTRYPOINT ["C:\\workspace\\NachoBot-Desktop-Pet-Adapter\\renderers\\live2d\\.venv\\Scripts\\python.exe", "-m", "desktop_pet_adapter", "--config", "config.toml", "--mode", "live"]
