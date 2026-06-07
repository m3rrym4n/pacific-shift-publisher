FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "3700", "--graceful-timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
