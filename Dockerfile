# halal-crypto-bot — 24/7 service (trading loop + Telegram control)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Persist portfolio / control / signal feed here — mount a volume at /app/data
VOLUME ["/app/data"]

# No inbound ports: Telegram is outbound long-polling only.
CMD ["python", "-m", "bot.serve"]
