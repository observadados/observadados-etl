#!/usr/bin/env python3
"""
Script de diagnóstico do Playwright no container Docker.
Rode com: python debug_playwright.py
"""
import sys, os, subprocess, platform

print("=" * 60)
print("DIAGNÓSTICO PLAYWRIGHT / CHROMIUM")
print("=" * 60)

# 1. Infos do ambiente
print(f"\n[1] Python: {sys.version}")
print(f"    OS: {platform.system()} {platform.release()}")
print(f"    Arch: {platform.machine()}")

# 2. Playwright instalado?
try:
    import playwright
    print(f"\n[2] playwright: {playwright.__version__} ✅")
except ImportError as e:
    print(f"\n[2] playwright: NÃO INSTALADO ❌ — {e}")
    sys.exit(1)

# 3. Binário do Chromium existe?
from playwright.sync_api import sync_playwright
try:
    with sync_playwright() as pw:
        info = pw.chromium.executable_path
        exists = os.path.exists(info)
        print(f"\n[3] Chromium path: {info}")
        print(f"    Existe no disco: {'✅' if exists else '❌ ARQUIVO NÃO ENCONTRADO'}")
        if not exists:
            print("    → Rode: playwright install chromium")
            sys.exit(1)
except Exception as e:
    print(f"\n[3] Erro ao verificar Chromium: {e}")
    sys.exit(1)

# 4. Tenta lançar o browser (etapa mais crítica)
print(f"\n[4] Tentando lançar o Chromium headless...")
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",   # <- reduz uso de memória
            ],
        )
        version = browser.version
        browser.close()
    print(f"    Chromium {version} iniciou com sucesso ✅")
except Exception as e:
    print(f"    ❌ FALHA ao lançar Chromium: {e}")
    sys.exit(1)

# 5. Testa acesso à página do CE
print(f"\n[5] Testando acesso à página da SSPDS-CE...")
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        resp = page.goto(
            "https://www.ce.gov.br/sspds/estatisticas/dados-detalhados/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        print(f"    HTTP status: {resp.status}")
        title = page.title()
        print(f"    Título da página: {title[:80]}")

        # Testa seletor do ce_cvli
        selector = "a[href*='CVLI'][href$='.xlsx']"
        els = page.query_selector_all(selector)
        print(f"    Links CVLI encontrados com seletor '{selector}': {len(els)}")
        for el in els[:3]:
            print(f"      → {el.get_attribute('href')}")

        ctx.close()
        browser.close()
    print("    Acesso à página: ✅")
except Exception as e:
    print(f"    ❌ Erro ao acessar página: {e}")

# 6. Memória disponível (Linux)
print(f"\n[6] Memória do sistema:")
try:
    with open("/proc/meminfo") as f:
        for line in f:
            if any(k in line for k in ("MemTotal", "MemFree", "MemAvailable", "SwapTotal", "SwapFree")):
                print(f"    {line.rstrip()}")
except FileNotFoundError:
    print("    (não disponível no Windows)")

# 7. /dev/shm size (crítico para Chromium no Docker)
print(f"\n[7] /dev/shm (shared memory):")
try:
    result = subprocess.run(["df", "-h", "/dev/shm"], capture_output=True, text=True)
    print("   ", result.stdout.strip().replace("\n", "\n    "))
except Exception as e:
    print(f"    {e}")

print("\n" + "=" * 60)
print("Diagnóstico concluído.")
