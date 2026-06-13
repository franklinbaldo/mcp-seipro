FROM python:3.11-slim

WORKDIR /app

# Dependencias de sistema para OCR (opcional)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-por poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar
COPY pyproject.toml README.md icon.png ./
COPY src ./src
RUN pip install --no-cache-dir .

# PORT triggers HTTP mode (Railway, Fly.io, etc.)
ENV PORT=8000
EXPOSE 8000

CMD ["todos"]
