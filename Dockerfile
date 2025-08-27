FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_binance.py .

# для Koyeb volume можно использовать
VOLUME ["/data"]
ENV STATE_FILE=/data/binance_funding_state.json

ENV POLL_SEC=30
ENV THRESHOLD=-1.0
ENV DOWN_STEP=0.25
ENV REBOUND_STEP=0.05
ENV REBOUND_START=-2.0
ENV ONLY_USDT=1
ENV SNAPSHOT_MODE=0
ENV UPDATE_POLL=2
ENV TIMEOUT_SEC=15

# прокси можно не задавать (OFF), но для Binance на Koyeb нужен
# ENV PROXY_URL=http://user:pass@host:port

CMD ["python", "bot_binance.py"]
