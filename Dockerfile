FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["gunicorn", "sync_server:app", "--workers", "1", "--threads", "2", "--timeout", "180", "--bind", "0.0.0.0:8080"]
