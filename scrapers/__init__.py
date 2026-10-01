from core.base_scraper import BaseScraper
from scrapers.scraper_rj import ScraperRJ
from scrapers.scraper_ce import ScraperCE

_SCRAPERS_REGISTRY = {
    # Rio de Janeiro
    'rj_feminicidio': ScraperRJ,
    'rj_series_historicas': ScraperRJ,
    'rj_elucidacao': ScraperRJ,
    'rj_policiais_mortos_servico': ScraperRJ,
    # Ceará — usa Playwright para contornar o bloqueio 403 do WAF da SSPDS-CE
    'ce_incendio': ScraperCE,
    'ce_salvamento': ScraperCE,
    'ce_animais': ScraperCE,
    'ce_arma_de_fogo': ScraperCE,
    'ce_crimes_sexuais': ScraperCE,
    'ce_cvp': ScraperCE,
    'ce_entorpecente': ScraperCE,
    'ce_furto': ScraperCE,
    'ce_homofobia_e_transfobia': ScraperCE,
    'ce_indigenas': ScraperCE,
    'ce_lei_maria_da_penha': ScraperCE,
    'ce_crime_raca_ou_cor': ScraperCE,
    'ce_cvli': ScraperCE,
}

def get_scraper_for(collector_key: str):
    """
    Retorna a classe de scraper adequada.
    Se não for encontrada, retorna a BaseScraper que atuará de forma genérica
    utilizando o css_selector.
    """
    return _SCRAPERS_REGISTRY.get(collector_key, BaseScraper)

