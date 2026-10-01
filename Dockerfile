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

# Copia dependências do build stage
COPY --from=builder /install /usr/local

# Copia o código-fonte
COPY . .

# Cria diretórios persistentes que serão montados como volumes
RUN mkdir -p /app/source-files

# Variáveis de ambiente (sobrescritas pelo Coolify via Environment Variables)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# O container não expõe porta — é um worker/cron, não um servidor web
CMD ["python", "main.py", "--publish-wordpress"]
