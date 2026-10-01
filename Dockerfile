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

# Copia dependências Python do build stage antes do playwright install-deps,
# pois o CLI do playwright precisa estar disponível para instalar as libs do sistema
COPY --from=builder /install /usr/local

# Instala o Chromium E todas as suas dependências de sistema automaticamente.
# --with-deps detecta a distro (Debian/Ubuntu) e instala os pacotes apt corretos.
# Isso substitui a listagem manual de libs, que é frágil e varia por versão do Debian.
RUN playwright install --with-deps chromium

# Copia o código-fonte
COPY . .

# Cria diretórios persistentes que serão montados como volumes
RUN mkdir -p /app/source-files

# Variáveis de ambiente (sobrescritas pelo Coolify via Environment Variables)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# O container não expõe porta — é um worker/cron, não um servidor web
CMD ["python", "main.py", "--publish-wordpress"]

