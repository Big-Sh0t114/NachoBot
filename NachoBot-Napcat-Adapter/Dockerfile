FROM python:3.13.5-slim
LABEL authors="infinitycat233"

# Copy uv and maim_message
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY maim_message /maim_message
COPY requirements.txt /requirements.txt

# Install requirements
RUN uv pip install --system --upgrade pip
RUN uv pip install --system -e /maim_message
RUN uv pip install --system -r /requirements.txt

WORKDIR /adapters

COPY . .

EXPOSE 8095

ENTRYPOINT ["python", "main.py"]