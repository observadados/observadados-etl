import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from typing import List, Dict, Optional

class BaseScraper:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        })

    def get_html(self, url: str) -> Optional[BeautifulSoup]:
        """
        Faz requisição GET e retorna o objeto BeautifulSoup.
        Usa timeout=(5, 25): 5s para conexão TCP (identifica rapidamente bloqueio por firewall/WAF)
        e 25s para recebimento dos dados.
        """
        try:
            response = self.session.get(url, timeout=(5, 25))
            if response.status_code in (401, 403):
                print(f"    [AVISO] Acesso bloqueado por WAF/IP (HTTP {response.status_code}). Pulando fonte.")
                return None
            response.raise_for_status()
            return BeautifulSoup(response.text, 'html.parser')
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError):
            print("    [AVISO] Conexão bloqueada por firewall ou timeout (IP descartado/inacessível). Pulando fonte.")
            return None
        except requests.exceptions.RequestException as e:
            print(f"    [AVISO] Falha ao acessar URL ({e}). Pulando fonte.")
            return None

    def discover_datasets(self, url: str, css_selector: str = None) -> List[Dict[str, str]]:
        """
        Método padrão para buscar arquivos em uma página se houver um seletor CSS.
        Caso contrário, as classes filhas devem sobrescrever com lógica específica.
        Retorna uma lista de dicionários com 'url' (do arquivo) e opcionalmente 'dataset_type' e 'filename'.
        """
        if not css_selector:
            raise NotImplementedError("Nenhum seletor CSS fornecido e método não sobrescrito na classe filha.")
            
        soup = self.get_html(url)
        if not soup:
            return []

        datasets = []
        for element in soup.select(css_selector):
            if element.has_attr('href'):
                real_url = urljoin(url, element['href'])
                
                # Heurística básica de tipo baseada no link ou texto
                texto_link = element.get_text().lower()
                dataset_type = "cvli" if "cvli" in texto_link else "geral"
                
                datasets.append({
                    "url": real_url,
                    "dataset_type": dataset_type,
                    "filename": real_url.split('/')[-1]
                })
        return datasets
