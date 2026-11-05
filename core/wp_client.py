import os
import re
import json
import sys
import sqlite3
from pathlib import Path
from datetime import datetime
import requests


from core.wp_resolver import WPLookupError, forget_cache, resolve_dataset_post

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _row_media_id(item: dict):
    arquivo = item.get("arquivo")
    return arquivo.get("id") if isinstance(arquivo, dict) else arquivo


def _media_filenames(wp_url: str, auth, media_ids: list) -> dict:
    """{media_id: nome do arquivo} para os anexos já vinculados ao post."""
    ids = [int(i) for i in media_ids if str(i).isdigit()]
    if not ids:
        return {}
    try:
        r = requests.get(
            f"{wp_url.rstrip('/')}/wp-json/wp/v2/media",
            params={"include": ",".join(map(str, ids)), "per_page": 100, "_fields": "id,source_url"},
            auth=auth,
            timeout=30,
        )
        if r.status_code != 200:
            return {}
        return {m["id"]: m.get("source_url", "").rsplit("/", 1)[-1] for m in r.json()}
    except requests.RequestException:
        return {}


def _same_file(existing_name: str | None, new_name: str) -> bool:
    """Compara nomes ignorando o sufixo `-N` que o WordPress acrescenta a uploads repetidos."""
    if not existing_name:
        return False
    strip = lambda n: re.sub(r"-\d+(\.[^.]+)$", r"\1", n.lower())
    return strip(existing_name) == strip(new_name)


def _cached_post_id(conn: sqlite3.Connection, collector_key: str, uf: str | None) -> int | None:
    """wp_post_id já gravado no SQLite para outra fonte do mesmo coletor (cache)."""
    if uf:
        row = conn.execute(
            "SELECT wp_post_id FROM sources WHERE collector_key = ? AND uf = ? AND wp_post_id IS NOT NULL LIMIT 1",
            (collector_key, uf),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT wp_post_id FROM sources WHERE collector_key = ? AND uf IS NULL AND wp_post_id IS NOT NULL LIMIT 1",
            (collector_key,),
        ).fetchone()
    return row[0] if row else None


def _load_env(env_file: str | Path | None = None, override: bool = False) -> Path | None:
    """
    Carrega variáveis de um arquivo .env. Sem argumento, usa OBSERVADADOS_ENV_FILE
    (se definido) ou `.env` na raiz do projeto. Com override=False, variáveis já
    presentes no ambiente têm precedência. Retorna o caminho carregado (ou None).
    """
    env_file = env_file or os.environ.get("OBSERVADADOS_ENV_FILE") or ".env"
    env_path = Path(env_file)
    if not env_path.is_absolute():
        env_path = PROJECT_ROOT / env_path
    if not env_path.exists():
        return None
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if override:
                os.environ[k.strip()] = v.strip()
            else:
                os.environ.setdefault(k.strip(), v.strip())
    return env_path


def publish_to_wordpress(
    conn: sqlite3.Connection,
    source_id: int,
    file_path: Path,
    metadata_json: str | None = None,
) -> tuple[int | None, int | None, str | None]:
    _load_env()
    wp_url = os.environ.get("WP_URL")
    wp_user = os.environ.get("WP_USER")
    wp_pass = os.environ.get("WP_APP_PASSWORD")

    if not wp_url or not wp_user or not wp_pass:
        print(
            "⚠️ Ignorando publicação no WordPress: credenciais incompletas no arquivo .env ou no ambiente.",
            file=sys.stderr,
        )
        return None, None, None

    print(
        f"📤 Enviando {file_path.name} para o WP...",
        end=" ",
        file=sys.stderr,
        flush=True,
    )

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT wp_title, wp_description, acf_subtitulo, acf_origem, 
               acf_frequencia, acf_formato, wp_post_id, collector_key, uf, url,
               wp_content, acf_tratamento
        FROM sources WHERE id = ?
    """,
        (source_id,),
    )
    source_data = cursor.fetchone()
    if not source_data:
        return None, None, None

    (
        wp_title,
        wp_description,
        acf_subtitulo,
        acf_origem,
        acf_frequencia,
        acf_formato,
        wp_post_id,
        collector_key,
        uf,
        source_url,
        wp_content,
        acf_tratamento,
    ) = source_data

    # O post certo é resolvido no WordPress (meta collector_key); o SQLite é só cache.
    # Isso acontece ANTES do upload, para não deixar mídia órfã se a busca falhar.
    try:
        wp_post_id = resolve_dataset_post(
            wp_url,
            (wp_user, wp_pass),
            collector_key,
            uf,
            cached_post_id=wp_post_id or _cached_post_id(conn, collector_key, uf),
            title=wp_title or f"Dados de {collector_key.upper()} - {uf}",
        )
    except WPLookupError as exc:
        print(f"\n❌ {exc}\n   Publicação de {file_path.name} cancelada (nenhum post criado).", file=sys.stderr)
        return None, None, None

    api_upload = f"{wp_url.rstrip('/')}/wp-json/observadados/v1/upload-dataset"
    data = {
        "uf": uf or "BR",
        "collector_key": collector_key or "generic",
        "filename": file_path.name,
    }

    try:
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "application/octet-stream")}
            response = requests.post(
                api_upload, auth=(wp_user, wp_pass), data=data, files=files, timeout=90
            )

        if response.status_code not in [200, 201]:
            print(
                f"ERRO upload customizado: {response.status_code} - {response.text}",
                file=sys.stderr,
            )
            return None, None, None

        upload_info = response.json()
        media_id = upload_info.get("id")
        if not media_id:
            return None, None, None

        print(f"OK (ID Mídia: {media_id})", file=sys.stderr)

        title = wp_title or f"Dados de {collector_key.upper()} - {uf}"
        description = wp_description or f"Arquivo de coleta automática."
        file_date = datetime.now().strftime("%d/%m/%Y")

        # Perfil v2: o JSON vai só em `metadados`; não anexamos mais HTML em `descricao`
        # (ver docs/profile-schema.md).
        new_file_item = {
            "descricao": "",
            "data": file_date,
            "arquivo": media_id,
            "formato": acf_formato or 5,
            "metadados": metadata_json or "{}",
        }

        existing_files = []
        existing_status = "draft"
        api_datasets = f"{wp_url.rstrip('/')}/wp-json/wp/v2/dataset"

        if wp_post_id:
            # O post foi resolvido há pouco; se agora não der para lê-lo, não criamos outro.
            get_response = requests.get(
                f"{api_datasets}/{wp_post_id}",
                params={"context": "edit"},
                auth=(wp_user, wp_pass),
                timeout=30,
            )
            if get_response.status_code != 200:
                print(
                    f"\n❌ Não foi possível ler o post {wp_post_id} ({get_response.status_code}); "
                    f"mídia {media_id} enviada mas não vinculada.",
                    file=sys.stderr,
                )
                return media_id, None, None
            post_info = get_response.json()
            existing_files = post_info.get("acf", {}).get("arquivos") or []
            existing_status = post_info.get("status", "publish")

        existing_names = _media_filenames(
            wp_url,
            (wp_user, wp_pass),
            [_row_media_id(item) for item in existing_files if isinstance(item, dict)],
        )

        merged_files = []
        for item in existing_files:
            if isinstance(item, dict):
                existing_file_id = _row_media_id(item)
                # Mesmo arquivo coletado de novo (ex.: --overwrite no mesmo mês): substitui a linha.
                if existing_file_id == media_id or _same_file(
                    existing_names.get(existing_file_id), file_path.name
                ):
                    continue
                merged_files.append(
                    {
                        "descricao": item.get("descricao", ""),
                        "data": item.get("data", ""),
                        "arquivo": existing_file_id,
                        "formato": item.get("formato", 5),
                        "metadados": item.get("metadados", "{}"),
                    }
                )
        merged_files.append(new_file_item)

        freq_map = {
            "mensal": "Mensal",
            "anual": "Anual",
            "semanal": "Semanal",
            "diaria": "Diária",
            "diária": "Diária",
        }
        freq_value = freq_map.get(str(acf_frequencia).strip().lower(), "Mensal")

        content_val = wp_content if wp_content else description
        tratamento_val = acf_tratamento if acf_tratamento else "Dados originais"
        
        # Lógica de Instituição vs Origem
        origem_str = acf_origem or f"Coletor {collector_key}"
        instituicao_id = None
        
        if acf_origem:
            try:
                search_url = f"{wp_url}/wp-json/wp/v2/instituicao?search={acf_origem}"
                search_resp = requests.get(search_url, auth=(wp_user, wp_pass), timeout=15)
                if search_resp.status_code == 200:
                    results = search_resp.json()
                    if results and len(results) > 0:
                        # Pega o ID do primeiro resultado retornado pela busca
                        instituicao_id = results[0]["id"]
            except Exception as e:
                print(f"⚠️ Erro ao buscar instituição '{acf_origem}': {e}", file=sys.stderr)

        acf_payload = {
            "subtitulo": acf_subtitulo or f"Coleta de fontes {uf}",
            "link": source_url,
            "frequencia": freq_value,
            "arquivos": merged_files,
            "tratamento": tratamento_val,
            "data_ultima_varredura": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        
        if instituicao_id:
            acf_payload["instituicao"] = instituicao_id
            acf_payload["origem"] = "" # Limpa o campo de texto caso passe a usar a relação
        else:
            acf_payload["origem"] = origem_str

        post_payload = {
            "title": title,
            "excerpt": description[:200] if description else "",
            "content": content_val,
            "status": existing_status,
            "acf": acf_payload,
            # Vínculo coletor → post, usado por resolve_dataset_post nas próximas coletas.
            "meta": {"collector_key": collector_key or "", "collector_uf": uf or ""},
        }

        final_post_id = wp_post_id
        headers = {"Content-Type": "application/json"}
        if wp_post_id:
            post_response = requests.post(
                f"{api_datasets}/{wp_post_id}",
                auth=(wp_user, wp_pass),
                headers=headers,
                data=json.dumps(post_payload),
                timeout=30,
            )
        else:
            post_response = requests.post(
                api_datasets,
                auth=(wp_user, wp_pass),
                headers=headers,
                data=json.dumps(post_payload),
                timeout=30,
            )

        if post_response.status_code in [200, 201]:
            new_post_id = post_response.json().get("id")
            if uf:
                conn.execute(
                    "UPDATE sources SET wp_post_id = ? WHERE collector_key = ? AND uf = ?",
                    (new_post_id, collector_key, uf),
                )
            else:
                conn.execute(
                    "UPDATE sources SET wp_post_id = ? WHERE collector_key = ? AND uf IS NULL",
                    (new_post_id, collector_key),
                )
            conn.commit()
            final_post_id = new_post_id
            forget_cache()
        else:
            print(
                f"\n❌ Falha ao salvar o dataset ({post_response.status_code}): {post_response.text[:300]}",
                file=sys.stderr,
            )
            return media_id, None, None

        return media_id, final_post_id, existing_status

    except Exception as e:
        print(f"ERRO WP: {e}", file=sys.stderr)
        return None, None, None


def update_last_scan_only(conn: sqlite3.Connection, source_id: int) -> bool:
    """
    Atualiza apenas o campo ACF 'data_ultima_varredura' no WordPress para o dataset correspondente,
    sem fazer upload de arquivo ou alterar outras informações.
    """
    _load_env()
    wp_url = os.environ.get("WP_URL")
    wp_user = os.environ.get("WP_USER")
    wp_pass = os.environ.get("WP_APP_PASSWORD")

    if not wp_url or not wp_user or not wp_pass:
        print(
            "⚠️ Ignorando atualização de varredura no WordPress: credenciais incompletas no arquivo .env ou no ambiente.",
            file=sys.stderr,
        )
        return False

    cursor = conn.cursor()
    cursor.execute("SELECT wp_post_id, collector_key, uf, wp_title FROM sources WHERE id = ?", (source_id,))
    row = cursor.fetchone()
    if not row:
        return False

    wp_post_id, collector_key, uf, wp_title = row
    try:
        wp_post_id = resolve_dataset_post(
            wp_url,
            (wp_user, wp_pass),
            collector_key,
            uf,
            cached_post_id=wp_post_id or _cached_post_id(conn, collector_key, uf),
            title=wp_title or f"Dados de {collector_key.upper()} - {uf}",
        )
    except WPLookupError as exc:
        print(f"⚠️ {exc}", file=sys.stderr)
        return False
    if wp_post_id:
        cursor.execute("UPDATE sources SET wp_post_id = ? WHERE id = ?", (wp_post_id, source_id))
        conn.commit()

    if not wp_post_id:
        print(
            f"⚠️ Não foi possível atualizar a varredura para a fonte {source_id}: nenhum post_id associado.",
            file=sys.stderr,
        )
        return False

    print(
        f"⏱️ Atualizando a data da última varredura no WP (Post ID: {wp_post_id})...",
        end=" ",
        file=sys.stderr,
        flush=True,
    )

    api_datasets = f"{wp_url.rstrip('/')}/wp-json/wp/v2/dataset"
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    post_payload = {
        "acf": {
            "data_ultima_varredura": current_time
        },
        "meta": {"collector_key": collector_key or "", "collector_uf": uf or ""},
    }

    try:
        headers = {"Content-Type": "application/json"}
        response = requests.post(
            f"{api_datasets}/{wp_post_id}",
            auth=(wp_user, wp_pass),
            headers=headers,
            data=json.dumps(post_payload),
            timeout=30,
        )
        if response.status_code == 200:
            print("OK", file=sys.stderr)
            return True
        else:
            print(f"ERRO: {response.status_code} - {response.text}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"ERRO: {e}", file=sys.stderr)
        return False
