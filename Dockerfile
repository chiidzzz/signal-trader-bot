# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOME=/app \
    PORT=8000

WORKDIR ${APP_HOME}

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p runtime && \
    chmod +x start.sh

EXPOSE 8000

CMD ["bash", "./start.sh"]
