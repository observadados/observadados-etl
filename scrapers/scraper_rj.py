from core.base_scraper import BaseScraper

class ScraperRJ(BaseScraper):
    def discover_datasets(self, url: str, css_selector: str = None):
        """
        Sobrescreve o método base se precisar de uma lógica customizada
        para o Rio de Janeiro. Caso não precise, pode só chamar o super().
        """
        # Se houver um seletor no banco, o comportamento base já funciona bem.
        if css_selector:
            return super().discover_datasets(url, css_selector)
        
        # Caso não haja seletor, podemos implementar regras hardcoded
        # Exemplo: O RJ possui CSVs na página principal de Dados Abertos
        # e as URLs geralmente contêm 'Base' ou 'Series'
        soup = self.get_html(url)
        if not soup:
            return []
        datasets = []
        for link in soup.select("a[href$='.csv']"):
            real_url = link['href']
            # Se for relativo
            if not real_url.startswith("http"):
                from urllib.parse import urljoin
                real_url = urljoin(url, real_url)
                
            dataset_type = "feminicidio" if "feminicidio" in real_url.lower() else "geral"
            datasets.append({
                "url": real_url,
                "dataset_type": dataset_type,
                "filename": real_url.split('/')[-1]
            })
            
        return datasets
