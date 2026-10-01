# ── Build stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Instala dependências de sistema necessárias para pandas/openpyxl
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Dependências de sistema para o Chromium headless (Playwright)
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Libs gráficas e de sistema exigidas pelo Chromium
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    # Dependências extras de renderização
    fonts-liberation \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copia dependências Python do build stage
COPY --from=builder /install /usr/local

# Instala o browser Chromium via playwright CLI
RUN playwright install chromium

# Copia o código-fonte
COPY . .

# Cria diretórios persistentes que serão montados como volumes
RUN mkdir -p /app/source-files

# Variáveis de ambiente (sobrescritas pelo Coolify via Environment Variables)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# O container não expõe porta — é um worker/cron, não um servidor web
CMD ["python", "main.py", "--publish-wordpress"]

