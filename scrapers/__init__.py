from core.base_scraper import BaseScraper
from scrapers.scraper_rj import ScraperRJ

_SCRAPERS_REGISTRY = {
    'rj_feminicidio': ScraperRJ,
    'rj_series_historicas': ScraperRJ,
    'rj_elucidacao': ScraperRJ,
    'rj_policiais_mortos_servico': ScraperRJ,
}

def get_scraper_for(collector_key: str):
    """
    Retorna a classe de scraper adequada.
    Se não for encontrada, retorna a BaseScraper que atuará de forma genérica
    utilizando o css_selector.
    """
    return _SCRAPERS_REGISTRY.get(collector_key, BaseScraper)
