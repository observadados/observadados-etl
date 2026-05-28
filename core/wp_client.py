import os
import json
import sys
import sqlite3
from pathlib import Path
from datetime import datetime
import requests


def _load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def publish_to_wordpress(
    conn: sqlite3.Connection,
    source_id: int,
    file_path: Path,
    metadata_json: str | None = None,
    metadata_html: str | None = None,
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

    if not wp_post_id:
        if uf:
            cursor.execute(
                """
                SELECT wp_post_id FROM sources 
                WHERE collector_key = ? AND uf = ? AND wp_post_id IS NOT NULL 
                LIMIT 1
            """,
                (collector_key, uf),
            )
        else:
            cursor.execute("""
                SELECT wp_post_id FROM sources 
                WHERE collector_key = ? AND uf IS NULL AND wp_post_id IS NOT NULL 
                LIMIT 1
            """)
        shared_row = cursor.fetchone()
        if shared_row:
            wp_post_id = shared_row[0]

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

        # O campo descricao agora conterá o nome original do arquivo (h3) e o html de metadados em seguida
        descricao_html = ""
        if metadata_html:
            descricao_html += f"\n{metadata_html}"

        new_file_item = {
            "descricao": descricao_html,
            "data": file_date,
            "arquivo": media_id,
            "formato": acf_formato or 5,
            "metadados": metadata_json or "{}",
        }

        existing_files = []
        existing_status = "draft"
        api_datasets = f"{wp_url.rstrip('/')}/wp-json/wp/v2/dataset"

        if wp_post_id:
            try:
                get_response = requests.get(
                    f"{api_datasets}/{wp_post_id}", auth=(wp_user, wp_pass), timeout=30
                )
                if get_response.status_code == 200:
                    post_info = get_response.json()
                    existing_files = post_info.get("acf", {}).get("arquivos") or []
                    existing_status = post_info.get("status", "publish")
                else:
                    wp_post_id = None
            except Exception:
                wp_post_id = None

        merged_files = []
        for item in existing_files:
            if isinstance(item, dict):
                existing_file_id = (
                    item.get("arquivo", {}).get("id")
                    if isinstance(item.get("arquivo"), dict)
                    else item.get("arquivo")
                )
                if (
                    existing_file_id == media_id
                    or item.get("descricao") == file_path.name
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

        return media_id, final_post_id, existing_status

    except Exception as e:
        print(f"ERRO WP: {e}", file=sys.stderr)
        return None, None, None
