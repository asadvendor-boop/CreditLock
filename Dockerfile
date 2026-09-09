FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for Chromium / Google Chrome headless
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-sandbox \
    fonts-liberation \
    wget \
    gnupg \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium

COPY pyproject.toml README.md AGENTS.md LICENSE /app/
COPY src/ /app/src/
COPY fixtures/ /app/fixtures/

RUN pip install --no-cache-dir -e "."

EXPOSE 8080

CMD ["uvicorn", "creditlock.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
