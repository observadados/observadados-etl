import os
import re
import sys
import shutil
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse, quote
from typing import Tuple

def safe_filename(value: str) -> str:
    clean = unquote(value).strip().replace('\\', '/').split('/')[-1]
    clean = re.sub(r'[^A-Za-z0-9._-]+', '_', clean).strip('._')
    return clean or 'source.bin'

def filename_for_source(source_url: str, override_name: str | None = None) -> str:
    if override_name:
        return safe_filename(override_name)
    parsed = urlparse(source_url)
    return safe_filename(Path(parsed.path).name)

def download_file(url: str, output_path: Path) -> Tuple[bool, int]:
    """
    Realiza o download de um arquivo para o output_path especificado.
    Retorna (sucesso: bool, tamanho_bytes: int).
    """
    # Garante que caracteres não-ASCII (acentos, espaços) sejam convertidos corretamente (%C3%A3, etc)
    safe_url = quote(url, safe=':/?&=+%')
    request = urllib.request.Request(safe_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with open(output_path, 'wb') as f:
                shutil.copyfileobj(response, f)
        
        size = os.path.getsize(output_path)
        return True, size
    except Exception as e:
        print(f"Erro ao baixar {url}: {e}", file=sys.stderr)
        return False, 0

def extract_zip_temp(zip_path: Path) -> Path:
    """
    Extrai temporariamente o arquivo .csv ou .xlsx contido no ZIP para fins de profiling.
    Retorna o Path do arquivo extraído.
    """
    import zipfile
    import tempfile
    if zip_path.suffix.lower() != '.zip':
        return zip_path
        
    print(f"📦 Arquivo ZIP detectado para profiling: {zip_path.name}. Extraindo temporariamente...")
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # Encontra o primeiro arquivo com extensão suportada (.csv, .xlsx, .xls)
            extracted_name = None
            for name in zf.namelist():
                if name.lower().endswith(('.csv', '.xlsx', '.xls')):
                    extracted_name = name
                    break
            
            if not extracted_name:
                if zf.namelist():
                    extracted_name = zf.namelist()[0]
                else:
                    raise ValueError("O arquivo ZIP está vazio.")
            
            # Determina a extensão do arquivo extraído
            internal_ext = Path(extracted_name).suffix.lower()
            if not internal_ext:
                internal_ext = '.csv' # fallback
            
            # Caminho temporário de destino
            temp_dir = Path(tempfile.gettempdir())
            target_path = temp_dir / f"temp_profile_{zip_path.stem}{internal_ext}"
            
            # Extrai o arquivo para a pasta temporária
            with zf.open(extracted_name) as source, open(target_path, 'wb') as target:
                shutil.copyfileobj(source, target)
                
            print(f"✅ Extraído temporariamente para: {target_path.name}")
            return target_path
    except Exception as e:
        print(f"❌ Erro ao extrair ZIP temporário: {e}", file=sys.stderr)
        raise e

