"""
Reprocessamento dos perfis (`metadados`, schema v2) de arquivos já publicados.

Percorre os posts `dataset` via REST, baixa cada arquivo do repeater `arquivos`,
gera o perfil v2 e atualiza somente o subcampo `metadados` de cada linha,
preservando os demais subcampos.

    python main.py --reprofile --dry-run                 # usa .env.local, não grava nada
    python main.py --reprofile --dataset 123             # grava só no post 123
    python main.py --reprofile --env-file .env.local --save-profiles perfis/

Por segurança, recusa WP_URL que não seja local (localhost, *.local, *.test,
*.localhost) a menos que --allow-remote seja passado.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from core.profiler import build_profile, _dumps
from core.wp_client import _load_env

PROFILABLE = (".csv", ".txt", ".xlsx", ".xls", ".zip")
LOCAL_SUFFIXES = (".local", ".localhost", ".test")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(LOCAL_SUFFIXES)


def _ref_id(value):
    """Converte o valor de um subcampo vindo da REST para o formato de escrita (IDs)."""
    if isinstance(value, dict):
        for key in ("id", "ID", "term_id", "value"):
            if key in value:
                return value[key]
        return value
    if isinstance(value, list):
        return [_ref_id(v) for v in value]
    return value


class WP:
    def __init__(self, url: str, user: str, password: str):
        self.base = url.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (user, password)

    def get(self, path: str, **params):
        r = self.s.get(f"{self.base}/wp-json/{path.lstrip('/')}", params=params, timeout=60)
        r.raise_for_status()
        return r

    def datasets(self, ids: list[int] | None):
        if ids:
            for i in ids:
                yield self.get(f"wp/v2/dataset/{i}", context="edit").json()
            return
        page = 1
        while True:
            r = self.get("wp/v2/dataset", per_page=50, page=page, status="any", context="edit")
            items = r.json()
            yield from items
            if page >= int(r.headers.get("X-WP-TotalPages", 1)) or not items:
                break
            page += 1

    def file_url(self, arquivo) -> str | None:
        if isinstance(arquivo, dict):
            if arquivo.get("url"):
                return arquivo["url"]
            arquivo = _ref_id(arquivo)
        if isinstance(arquivo, str) and arquivo.startswith("http"):
            return arquivo
        try:
            media_id = int(arquivo)
        except (TypeError, ValueError):
            return None
        if not media_id:
            return None
        return self.get(f"wp/v2/media/{media_id}", _fields="source_url").json().get("source_url")

    def download(self, url: str, dest: Path) -> None:
        with self.s.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            # O Apache da produção serve os arquivos com Content-Encoding: gzip;
            # sem decode_content, r.raw grava os bytes ainda comprimidos.
            r.raw.decode_content = True
            with open(dest, "wb") as f:
                shutil.copyfileobj(r.raw, f, 1024 * 1024)

    def required_acf(self) -> list[str]:
        if not hasattr(self, "_required"):
            try:
                r = self.s.options(f"{self.base}/wp-json/wp/v2/dataset", timeout=60)
                props = r.json()["schema"]["properties"]["acf"]["properties"]
                self._required = [k for k, v in props.items() if v.get("required")]
            except Exception:
                self._required = ["link", "frequencia", "tratamento"]
        return self._required

    def update_arquivos(self, post_id: int, acf: dict, rows: list[dict]) -> None:
        # A validação REST da ACF exige no payload os campos marcados como obrigatórios
        # (ex.: `link`), então os reenviamos como vieram, junto com `arquivos`. Os demais
        # campos ficam de fora para não esbarrar em validações (ex.: `categorias` vazia
        # com mínimo de 1 item) e não são alterados.
        payload = {k: _ref_id(acf[k]) for k in self.required_acf() if k in acf and k != "arquivos"}
        payload["arquivos"] = rows
        r = self.s.post(
            f"{self.base}/wp-json/wp/v2/dataset/{post_id}",
            json={"acf": payload},
            timeout=120,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")


def _profile_download(path: Path, display_name: str) -> dict:
    if path.suffix.lower() == ".zip":
        from core.downloader import extract_zip_temp

        inner = extract_zip_temp(path)
        try:
            return build_profile(inner, display_name=inner.name.removeprefix("temp_profile_"))
        finally:
            if inner != path and inner.exists():
                inner.unlink()
    return build_profile(path, display_name=display_name)


def run_reprofile(
    dataset_ids: list[int] | None = None,
    dry_run: bool = False,
    env_file: str = ".env.local",
    allow_remote: bool = False,
    save_dir: str | None = None,
) -> int:
    loaded = _load_env(env_file, override=True)
    if not loaded:
        print(f"❌ Arquivo de ambiente não encontrado: {env_file}", file=sys.stderr)
        return 2
    wp_url = os.environ.get("WP_URL", "")
    user = os.environ.get("WP_USER")
    password = os.environ.get("WP_APP_PASSWORD")
    if not wp_url or not user or not password:
        print(f"❌ WP_URL/WP_USER/WP_APP_PASSWORD incompletos em {loaded.name}", file=sys.stderr)
        return 2
    if not is_local_url(wp_url) and not allow_remote:
        print(
            f"❌ Recusando --reprofile contra {wp_url} (não é um WordPress local). "
            "Use --env-file .env.local ou passe --allow-remote conscientemente.",
            file=sys.stderr,
        )
        return 2

    mode = "DRY-RUN (nada será gravado)" if dry_run else "GRAVANDO"
    print(f"🔁 Reprocessando perfis em {wp_url} [{loaded.name}] — {mode}")
    wp = WP(wp_url, user, password)
    out_dir = Path(save_dir) if save_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    totals = {"posts": 0, "files": 0, "profiled": 0, "skipped": 0, "errors": 0, "updated": 0}
    tmp = Path(tempfile.mkdtemp(prefix="observadados-reprofile-"))
    try:
        for post in wp.datasets(dataset_ids):
            totals["posts"] += 1
            pid = post.get("id")
            title = (post.get("title") or {}).get("raw") or (post.get("title") or {}).get("rendered", "")
            acf = post.get("acf") or {}
            rows = acf.get("arquivos") or []
            print(f"\n📁 Dataset {pid}: {title} ({len(rows)} arquivo(s))")
            if not isinstance(rows, list) or not rows:
                continue

            new_rows, changed = [], False
            for idx, row in enumerate(rows):
                new_row = {k: _ref_id(v) for k, v in row.items()} if isinstance(row, dict) else row
                new_rows.append(new_row)
                if not isinstance(row, dict):
                    continue
                totals["files"] += 1
                try:
                    url = wp.file_url(row.get("arquivo"))
                except Exception as e:
                    print(f"   [{idx}] ⚠️ não foi possível resolver o arquivo: {e}")
                    totals["errors"] += 1
                    continue
                if not url:
                    print(f"   [{idx}] ⏩ sem arquivo associado")
                    totals["skipped"] += 1
                    continue
                name = unquote(Path(urlparse(url).path).name)
                if Path(name).suffix.lower() not in PROFILABLE:
                    print(f"   [{idx}] ⏩ {name}: formato não perfilável")
                    totals["skipped"] += 1
                    continue
                # subpasta por linha, mantendo o nome original (usado no perfil de ZIPs)
                (tmp / f"{pid}-{idx}").mkdir(exist_ok=True)
                local = tmp / f"{pid}-{idx}" / name
                try:
                    t0 = time.perf_counter()
                    wp.download(url, local)
                    profile = _profile_download(local, name)
                    js = _dumps(profile)
                    dt = time.perf_counter() - t0
                except Exception as e:
                    print(f"   [{idx}] ❌ {name}: {e}")
                    totals["errors"] += 1
                    continue
                finally:
                    if local.exists():
                        local.unlink()
                totals["profiled"] += 1
                types = {}
                for f in profile["fields"]:
                    types[f["type"]] = types.get(f["type"], 0) + 1
                print(
                    f"   [{idx}] ✅ {name}: {profile['rows']} linhas × {profile['columns']} colunas, "
                    f"{len(js.encode('utf-8')) / 1024:.1f} KB, {dt:.1f}s, tipos={types}"
                )
                if out_dir:
                    (out_dir / f"{pid}-{idx}-{Path(name).stem}.json").write_text(
                        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                new_row["metadados"] = js
                changed = True

            if changed and not dry_run:
                try:
                    wp.update_arquivos(pid, acf, new_rows)
                    totals["updated"] += 1
                    print(f"   💾 post {pid} atualizado")
                except Exception as e:
                    totals["errors"] += 1
                    print(f"   ❌ falha ao atualizar post {pid}: {e}")
    except requests.RequestException as e:
        print(f"❌ Erro de comunicação com {wp_url}: {e}", file=sys.stderr)
        totals["errors"] += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(
        f"\nResumo: {totals['posts']} posts, {totals['files']} arquivos, {totals['profiled']} perfilados, "
        f"{totals['skipped']} ignorados, {totals['errors']} erros, {totals['updated']} posts atualizados"
        + (" (dry-run)" if dry_run else "")
    )
    return 1 if totals["errors"] else 0
