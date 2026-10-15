FROM python:3.13-windowsservercore-ltsc2022

WORKDIR C:/workspace/NachoBot-UniversalVC-Adapter

# ProcTap and the virtual audio-device integration are Windows-oriented.
RUN python -m pip install --no-cache-dir --upgrade pip uv

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .

COPY --from=nachobot /ncnk_message C:/workspace/NachoBot/ncnk_message

ENTRYPOINT ["C:\\workspace\\NachoBot-UniversalVC-Adapter\\.venv\\Scripts\\python.exe", "main.py"]
