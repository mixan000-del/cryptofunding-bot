FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

VOLUME ["/data"]
ENV STATE_FILE=/data/funding_state.json
ENV POLL_SEC=30
ENV THRESHOLD=-1.0
ENV DOWN_STEP=0.25
ENV REBOUND_STEP=0.05
ENV REBOUND_START=-2.0
ENV ONLY_USDT=1
ENV SNAPSHOT_MODE=0

CMD ["python", "bot.py"]
