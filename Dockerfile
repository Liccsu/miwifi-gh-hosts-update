FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh

RUN addgroup -S -g 10001 appuser && \
    adduser -S -D -H -u 10001 -G appuser appuser && \
    mkdir -p /data && \
    chown -R appuser:appuser /app /data && \
    apk add --no-cache su-exec && \
    chmod +x /entrypoint.sh

EXPOSE 8571

ENTRYPOINT ["/entrypoint.sh"]
