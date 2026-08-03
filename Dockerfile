FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Taipei

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

# 容器裡只裝監控。下單流程（.[buy]）需要瀏覽器，而 `momo-watch login` 必須開
# 有畫面的視窗手動登入 —— 那一步在你自己的機器上做，把產生的 storage_state.json
# 掛進來才有意義。
RUN pip install --no-cache-dir .

# 資料庫掛在 volume 上，重啟才不會忘記上一次的狀態
ENV MOMO_DB_PATH=/data/momo_watch.db
VOLUME ["/data"]

ENTRYPOINT ["momo-watch"]
CMD ["run"]
