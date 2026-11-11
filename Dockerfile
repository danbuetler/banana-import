FROM python:3.12-slim

RUN useradd --create-home appuser
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpoppler-cpp-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY . .
RUN mkdir -p /app/data && chown appuser /app/data
USER appuser

EXPOSE 8500

CMD ["python", "app.py"]
