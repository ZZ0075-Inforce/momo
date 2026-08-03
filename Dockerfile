FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Taipei

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

# 資料庫掛在 volume 上，重啟才不會忘記上一次的狀態
ENV MOMO_DB_PATH=/data/momo_watch.db
VOLUME ["/data"]

ENTRYPOINT ["momo-watch"]
CMD ["run"]
