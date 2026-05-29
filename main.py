import argparse
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')
import shutil
from pathlib import Path
from core.db_manager import (
    init_db,
    sync_csv_to_db,
    load_sources_from_db,
    should_skip_frequency,
    record_download_history,
)
from core.downloader import download_file, filename_for_source
from core.wp_client import publish_to_wordpress, update_last_scan_only
from core.base_scraper import BaseScraper
from core.profiler import profile_file
from core.mailer import send_admin_email
from scrapers import get_scraper_for


def main() -> int:
    parser = argparse.ArgumentParser(description="Orquestrador ETL para ObservaDados.")
    parser.add_argument(
        "--output-dir",
        default="source-files",
        help="Diretorio de saida dos arquivos baixados.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Baixa novamente mesmo quando o arquivo ja existe.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Limpa o diretorio de saida antes de iniciar o download das fontes.",
    )
    parser.add_argument(
        "--publish-wordpress",
        action="store_true",
        help="Publica os arquivos baixados na biblioteca de midia e atualiza o dataset correspondente no WordPress.",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Limpa completamente o banco de dados SQLite.",
    )
    args = parser.parse_args()

    # 1. Setup inicial
    conn = init_db()
    if args.reset_db:
        print("🧹 Limpando banco de dados (tabelas sources e download_history)...")
        conn.execute("DELETE FROM download_history;")
        conn.execute("DELETE FROM sources;")
        conn.commit()

    print("🔄 Sincronizando fontes (CSV -> SQLite)...")
    sync_csv_to_db(conn)
    active_sources = load_sources_from_db(conn)

    output_dir = Path(args.output_dir).resolve()

    if args.clean and output_dir.exists():
        print(f"🧹 Limpando o diretório de saída: {output_dir}...")
        for item in output_dir.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            except Exception as exc:
                print(f"⚠️ Erro ao limpar {item.name}: {exc}")

    output_dir.mkdir(parents=True, exist_ok=True)

    published_items = []

    # 2. Orquestração do Extract
    for source in active_sources:
        print(f"\n--- Processando {source.collector_key} ({source.uf}) ---")

        # Validar frequência
        if (
            should_skip_frequency(conn, source.id, source.acf_frequencia)
            and not args.overwrite
        ):
            print(f"⏩ Frequência ainda não atingida. Ignorando.")
            continue

        uf_dir = output_dir / (source.uf or "BR")
        uf_dir.mkdir(parents=True, exist_ok=True)

        from datetime import datetime
        from core.downloader import safe_filename
        now_ts = datetime.now().strftime("%Y%m")
        safe_key = source.collector_key.replace('_', '-')
        
        # Se o usuário preencheu a coluna filename na planilha, usamos como sufixo para diferenciar
        if source.filename:
            custom_suffix = safe_filename(source.filename).split('.')[0]
            safe_key = f"{safe_key}-{custom_suffix}"
        
        # Descobrir links reais
        final_downloads = []
        is_direct_file = source.url.lower().endswith(('.csv', '.xlsx', '.xls', '.pdf', '.zip'))
        
        if is_direct_file:
            orig_name = filename_for_source(source.url, source.filename)
            ext = Path(orig_name).suffix
            final_downloads.append({
                "url": source.url,
                "filename": f"{safe_key}-{now_ts}{ext}"
            })
        else:
            print(f"🔍 Página dinâmica detectada. Tentando mapear arquivos...")
            scraper_class = get_scraper_for(source.collector_key)
            scraper = scraper_class(source.url)
            
            try:
                found_datasets = scraper.discover_datasets(source.url, source.css_selector)
                for idx, ds in enumerate(found_datasets):
                    orig_name = ds.get("filename") or filename_for_source(ds["url"])
                    ext = Path(orig_name).suffix
                    if len(found_datasets) > 1:
                        new_name = f"{safe_key}-{idx+1}-{now_ts}{ext}"
                    else:
                        new_name = f"{safe_key}-{now_ts}{ext}"
                        
                    final_downloads.append({
                        "url": ds["url"],
                        "filename": new_name
                    })
            except Exception as e:
                print(f"❌ Erro ao raspar página: {e}")
                record_download_history(conn, source.id, "error", error_message=str(e))
                continue

        # 3. Processar cada arquivo encontrado (Download & Load)
        if not final_downloads:
            print("⚠️ Nenhum arquivo encontrado para baixar.")
            record_download_history(
                conn, source.id, "error", error_message="Nenhum arquivo encontrado"
            )
            continue

        for item in final_downloads:
            out_path = uf_dir / item["filename"]

            # Checar se o arquivo já existe e se já existe post no WP para apenas atualizar a data de varredura
            file_exists = out_path.exists()
            cursor = conn.cursor()
            cursor.execute("SELECT wp_post_id FROM sources WHERE id = ?", (source.id,))
            existing_post = cursor.fetchone()
            wp_post_id = existing_post[0] if existing_post else None

            if file_exists and not args.overwrite:
                print(
                    f"⏩ Arquivo já existe e --overwrite não foi passado: {item['filename']}"
                )
                if args.publish_wordpress and wp_post_id:
                    update_last_scan_only(conn, source.id)
                    record_download_history(conn, source.id, "skipped", out_path.stat().st_size)
                    continue
                
                success = True
                size = out_path.stat().st_size
            else:
                print(f"⬇️ Baixando {item['url']}...")
                success, size = download_file(item["url"], out_path)

            if success:
                print(f"📊 Extraindo metadados...")
                metadata_json, metadata_html = "{}", "<p>Erro ao extrair metadados.</p>"
                
                # Se for ZIP, extrai temporariamente para profiling
                if out_path.suffix.lower() == '.zip':
                    from core.downloader import extract_zip_temp
                    temp_extracted = None
                    try:
                        temp_extracted = extract_zip_temp(out_path)
                        metadata_json, metadata_html = profile_file(temp_extracted)
                    except Exception as e:
                        print(f"⚠️ Erro ao gerar perfil para o ZIP temporário: {e}")
                        metadata_json, metadata_html = "{}", f"<p>Erro ao extrair metadados do ZIP: {e}</p>"
                    finally:
                        if temp_extracted and temp_extracted.exists():
                            try:
                                temp_extracted.unlink()
                            except Exception as ex:
                                print(f"⚠️ Erro ao remover arquivo temporário: {ex}")
                else:
                    metadata_json, metadata_html = profile_file(out_path)
                
                # Enviar pro WP apenas se a flag estiver ativa
                if args.publish_wordpress:
                    # Verificar se o post_id já existia no banco para marcar a ação como "Atualizado"
                    cursor = conn.cursor()
                    cursor.execute("SELECT wp_post_id FROM sources WHERE id = ?", (source.id,))
                    existing_post = cursor.fetchone()
                    was_update = existing_post and existing_post[0] is not None
                    
                    media_id, post_id, status = publish_to_wordpress(
                        conn, source.id, out_path, metadata_json, metadata_html
                    )
                    
                    if post_id:
                        published_items.append({
                            'collector_key': source.collector_key,
                            'uf': source.uf or "BR",
                            'filename': out_path.name,
                            'action': "Atualizado" if was_update else "Criado",
                            'status': status or "draft",
                            'wp_post_id': post_id
                        })

                    if media_id:
                        record_download_history(
                            conn, source.id, "downloaded", size, wp_media_id=media_id
                        )
                    else:
                        record_download_history(
                            conn,
                            source.id,
                            "downloaded_wp_error",
                            size,
                            error_message="Erro no WP",
                        )
                else:
                    print(f"✅ Arquivo salvo localmente. (WordPress ignorado)")
                    print(f"📄 Preview JSON: {metadata_json[:300]}...")
                    record_download_history(conn, source.id, "downloaded", size)
            else:
                record_download_history(
                    conn, source.id, "error", error_message="Falha no download HTTP"
                )

    # 4. Enviar e-mail de resumo para o admin
    if args.publish_wordpress and published_items:
        send_admin_email(published_items)

    return 0


if __name__ == "__main__":
    sys.exit(main())
