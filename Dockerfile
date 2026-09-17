FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY cliper ./cliper
RUN uv sync --frozen --no-dev
ENV CLIPER_DATA=/data
VOLUME ["/data"]
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "cliper", "--host", "0.0.0.0", "--port", "8000", "--no-browser"]
