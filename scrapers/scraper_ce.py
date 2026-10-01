from __future__ import annotations

import os
from typing import List, Dict
from urllib.parse import urljoin

from core.base_scraper import BaseScraper


class ScraperCE(BaseScraper):
    """
    Scraper para o portal de estatísticas da SSPDS-CE.

    O site https://www.ce.gov.br/sspds/estatisticas/dados-detalhados/ bloqueia
    requisições simples de data centers com HTTP 403 (WAF/Cloudflare).
    Esta classe usa Playwright (browser Chromium headless real) para renderizar
    a página, contornando o bloqueio, e depois extrai os links via CSS selector.
    """

    # Flag de ambiente: em container Docker/CI costuma não ter sandbox
    _IN_CONTAINER = os.path.exists("/.dockerenv") or os.environ.get("PLAYWRIGHT_NO_SANDBOX")

    def discover_datasets(self, url: str, css_selector: str = None) -> List[Dict[str, str]]:
        if not css_selector:
            raise NotImplementedError(
                "ScraperCE exige um css_selector para identificar os links de download."
            )

        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        datasets: List[Dict[str, str]] = []

        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",   # usa /tmp em vez de /dev/shm (evita crash em Docker)
            "--disable-gpu",
        ]

        # Em container com pouca memória, single-process reduz uso de RAM
        if self._IN_CONTAINER:
            launch_args.append("--single-process")

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    headless=True,
                    args=launch_args,
                )
            except Exception as e:
                raise RuntimeError(
                    f"Falha ao iniciar o Chromium. "
                    f"Verifique se playwright install --with-deps chromium foi executado. "
                    f"Detalhe: {e}"
                ) from e

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="pt-BR",
            )

            page = context.new_page()

            try:
                # "load" aguarda todos os recursos + scripts executados,
                # necessário pois o site do CE injeta os links via JavaScript
                response = page.goto(url, wait_until="load", timeout=60_000)
                status = response.status if response else "?"
                title  = page.title() or "(sem título)"
                print(f"    [HTTP {status}] {title[:70]}")

                if response and response.status in (401, 403):
                    print("    [AVISO] Acesso bloqueado por WAF/IP (HTTP 403). Pulando fonte.")
                    return []

                # Aguarda o seletor aparecer no DOM (até 30 s)
                try:
                    page.wait_for_selector(css_selector, timeout=30_000)
                except PWTimeout:
                    # Nenhum elemento encontrado — loga o HTML da área relevante para debug
                    snippet = page.inner_html("body")[:500].replace("\n", " ")
                    print(f"    [WARN] Seletor nao encontrado apos 30 s: {css_selector}")
                    print(f"    [HTML] {snippet}")
                    return []

                elements = page.query_selector_all(css_selector)
                print(f"    [OK] {len(elements)} link(s) encontrado(s) com: {css_selector}")
                for el in elements:
                    href = el.get_attribute("href")
                    if not href:
                        continue
                    real_url = urljoin(url, href)
                    datasets.append(
                        {
                            "url": real_url,
                            "filename": real_url.split("/")[-1],
                        }
                    )

            finally:
                context.close()
                browser.close()

        return datasets

