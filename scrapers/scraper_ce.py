from __future__ import annotations

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

    def discover_datasets(self, url: str, css_selector: str = None) -> List[Dict[str, str]]:
        if not css_selector:
            raise NotImplementedError(
                "ScraperCE exige um css_selector para identificar os links de download."
            )

        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        datasets: List[Dict[str, str]] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                # Simula viewport real para nao ser detectado como headless
                viewport={"width": 1280, "height": 800},
                locale="pt-BR",
            )

            page = context.new_page()

            try:
                # Aguarda ate o DOM estar estavel (networkidle pode travar em sites lentos)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)

                # Aguarda ate que pelo menos um elemento que bata com o seletor apareca,
                # ou ate 15 s (evita espera infinita em paginas sem os links)
                try:
                    page.wait_for_selector(css_selector, timeout=15000)
                except PWTimeout:
                    # Se nao encontrar nada dentro do prazo, retorna lista vazia
                    return []

                elements = page.query_selector_all(css_selector)
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
