FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY app/ ./app/

RUN addgroup -S -g 10001 appuser && \
    adduser -S -D -H -u 10001 -G appuser appuser && \
    chown -R appuser:appuser /app

USER appuser

ENTRYPOINT ["python", "-m", "app"]
