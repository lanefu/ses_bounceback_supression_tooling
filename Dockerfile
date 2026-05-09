FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SES_BOUNCE_CONFIG=/data/config/ses-bounce.toml

WORKDIR /app

RUN groupadd --gid 10001 sesbounce \
    && useradd --uid 10001 --gid sesbounce --home-dir /app --shell /usr/sbin/nologin sesbounce

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY config.example.toml ./config.example.toml

RUN mkdir -p /data/config /data/db \
    && chown -R sesbounce:sesbounce /app /data

USER sesbounce

EXPOSE 8000

CMD ["python", "web_service.py", "--config", "/data/config/ses-bounce.toml"]
