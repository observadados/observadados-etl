from __future__ import annotations

import csv
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / 'collectors.db'
CSV_PATH = Path(__file__).resolve().parent.parent / 'static-sources' / 'sources.csv'

@dataclass
class SourceFile:
    collector_key: str
    url: str
    filename: str | None = None
    uf: str | None = None
    is_active: int = 1
    wp_title: str | None = None
    wp_description: str | None = None
    acf_subtitulo: str | None = None
    acf_origem: str | None = None
    acf_frequencia: str | None = None
    acf_formato: int | None = 5
    wp_post_id: int | None = None
    css_selector: str | None = None
    wp_content: str | None = None
    acf_tratamento: str | None = None
    id: int | None = None

def _infer_uf(key: str) -> str:
    parts = key.split('_')
    uf = parts[0].upper() if parts else 'BR'
    valid_ufs = {'AC', 'AL', 'AM', 'AP', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MT', 'MS', 'MG', 'PA', 'PB', 'PR', 'PE', 'PI', 'RJ', 'RN', 'RS', 'RO', 'RR', 'SC', 'SP', 'SE', 'TO'}
    if uf in valid_ufs:
        return uf
    return 'BR'

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collector_key TEXT NOT NULL,
            uf TEXT NOT NULL,
            url TEXT NOT NULL,
            filename TEXT,
            wp_title TEXT,
            wp_description TEXT,
            acf_subtitulo TEXT,
            acf_origem TEXT,
            acf_frequencia TEXT,
            acf_formato INTEGER,
            wp_post_id INTEGER,
            css_selector TEXT,
            wp_content TEXT,
            acf_tratamento TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            size_bytes INTEGER DEFAULT 0,
            error_message TEXT,
            wp_media_id INTEGER,
            visited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(source_id) REFERENCES sources(id)
        );
    """)
    conn.commit()
    return conn

def sync_csv_to_db(conn: sqlite3.Connection) -> None:
    if not CSV_PATH.exists():
        print(f"⚠️ Arquivo CSV não encontrado em: {CSV_PATH}", file=sys.stderr)
        return
        
    try:
        with open(CSV_PATH, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f, delimiter=';'))
    except UnicodeDecodeError:
        with open(CSV_PATH, 'r', encoding='cp1252') as f:
            rows = list(csv.DictReader(f, delimiter=';'))

    for row in rows:
        collector_key = row['collector_key']
        uf = row.get('uf') or _infer_uf(collector_key)
        url = row['url']
        filename = row.get('filename') or None
        is_active_raw = row.get('is_active', '').strip()
        is_active = int(is_active_raw) if is_active_raw else 1
        wp_title = row.get('wp_title') or None
        wp_description = row.get('wp_description') or None
        acf_subtitulo = row.get('acf_subtitulo') or None
        acf_origem = row.get('acf_origem') or None
        acf_frequencia = row.get('acf_frequencia') or None
        css_selector = row.get('css_selector') or None
        wp_content = row.get('wp_content') or None
        acf_tratamento = row.get('acf_tratamento') or None
        
        try:
            acf_formato = int(row['acf_formato']) if row.get('acf_formato') else 5
        except ValueError:
            acf_formato = 5
            
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM sources WHERE collector_key = ? AND url = ?",
            (collector_key, url)
        )
        existing = cursor.fetchone()
        
        if existing:
            source_id = existing[0]
            conn.execute("""
                UPDATE sources 
                SET uf = ?, filename = ?, wp_title = ?, wp_description = ?, 
                    acf_subtitulo = ?, acf_origem = ?, acf_frequencia = ?, 
                    acf_formato = ?, css_selector = ?, wp_content = ?, acf_tratamento = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (uf, filename, wp_title, wp_description, acf_subtitulo, 
                  acf_origem, acf_frequencia, acf_formato, css_selector, wp_content, acf_tratamento, is_active, source_id))
        else:
            conn.execute("""
                INSERT INTO sources (
                    collector_key, uf, url, filename, wp_title, wp_description,
                    acf_subtitulo, acf_origem, acf_frequencia, acf_formato, css_selector, wp_content, acf_tratamento, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (collector_key, uf, url, filename, wp_title, wp_description,
                  acf_subtitulo, acf_origem, acf_frequencia, acf_formato, css_selector, wp_content, acf_tratamento, is_active))
    conn.commit()

def load_sources_from_db(conn: sqlite3.Connection) -> list[SourceFile]:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, collector_key, uf, url, filename, is_active, wp_title, wp_description,
               acf_subtitulo, acf_origem, acf_frequencia, acf_formato, wp_post_id, css_selector, wp_content, acf_tratamento
        FROM sources
        WHERE is_active = 1
    """)
    sources = []
    for row in cursor.fetchall():
        sources.append(SourceFile(
            id=row[0],
            collector_key=row[1],
            uf=row[2],
            url=row[3],
            filename=row[4],
            is_active=row[5],
            wp_title=row[6],
            wp_description=row[7],
            acf_subtitulo=row[8],
            acf_origem=row[9],
            acf_frequencia=row[10],
            acf_formato=row[11],
            wp_post_id=row[12],
            css_selector=row[13],
            wp_content=row[14],
            acf_tratamento=row[15]
        ))
    return sources

def _parse_sqlite_timestamp(ts_str: str) -> datetime:
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%SZ'):
        try:
            return datetime.strptime(ts_str.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return datetime.now(tz=UTC)

def should_skip_frequency(conn: sqlite3.Connection, source_id: int, acf_frequencia: str | None) -> bool:
    if not acf_frequencia:
        return False
        
    freq_str = str(acf_frequencia).strip().lower()
    
    freq_days = {
        'diaria': 1,
        'diária': 1,
        'semanal': 7,
        'mensal': 30,
        'anual': 365
    }
    
    days_threshold = freq_days.get(freq_str)
    if days_threshold is None:
        return False
        
    cursor = conn.cursor()
    cursor.execute("""
        SELECT visited_at FROM download_history
        WHERE source_id = ?
          AND status IN ('downloaded', 'skipped')
          AND (error_message IS NULL OR error_message != 'Frequência de coleta não atingida')
        ORDER BY visited_at DESC LIMIT 1;
    """, (source_id,))
    
    row = cursor.fetchone()
    if not row:
        return False
        
    last_visited_str = row[0]
    last_visited = _parse_sqlite_timestamp(last_visited_str)
    now = datetime.now(tz=UTC)
    
    elapsed = now - last_visited
    if elapsed.days < days_threshold:
        return True
        
    return False

def record_download_history(conn: sqlite3.Connection, source_id: int, status: str, size_bytes: int = 0, error_message: str | None = None, wp_media_id: int | None = None) -> None:
    conn.execute("""
        INSERT INTO download_history (source_id, status, size_bytes, error_message, wp_media_id)
        VALUES (?, ?, ?, ?, ?)
    """, (source_id, status, size_bytes, error_message, wp_media_id))
    conn.commit()
