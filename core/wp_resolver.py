"""
Resolve qual post `dataset` do WordPress corresponde a um coletor.

O vínculo coletor → post fica no próprio WordPress (post meta `collector_key` e
`collector_uf`, registrados pelo tema em `inc/dataset-collector.php`). O
`wp_post_id` do SQLite local é só um cache: ele pode ter sido apagado
(`--reset-db`), ou ter vindo de outro ambiente (local x produção).

Ordem de busca:
  1. meta `collector_key` (+ `collector_uf`)          → fonte da verdade
  2. `wp_post_id` em cache, se o post existir e não pertencer a outro coletor
  3. título exato, para posts antigos ainda sem meta  → adotado e marcado
  4. nada encontrado                                  → None (o chamador cria)

Qualquer falha de comunicação ou ambiguidade levanta `WPLookupError`: nesse caso
o chamador NÃO deve criar post, para não gerar duplicados.
"""

from __future__ import annotations

import html
import re
import sys

import requests

STATUSES = "publish,draft,pending,private,future"
STATUS_RANK = {"publish": 0, "private": 1, "future": 2, "pending": 3, "draft": 4}

_all_datasets_cache: dict[str, list[dict]] = {}


class WPLookupError(Exception):
    """Não foi possível decidir com segurança qual post usar."""


def _norm_title(value: str | None) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"[‐-―]", "-", value)  # travessões → hífen
    value = re.sub(r"\s+", " ", value).strip().casefold()
    return value


def _title_raw(post: dict) -> str:
    title = post.get("title") or {}
    return title.get("raw") or title.get("rendered") or ""


def _pick(posts: list[dict]) -> dict:
    """Prefere publicado; em empate, o mais antigo."""
    return sorted(posts, key=lambda p: (STATUS_RANK.get(p.get("status"), 9), p.get("date", ""), p["id"]))[0]


def _meta(post: dict) -> dict:
    meta = post.get("meta")
    if not isinstance(meta, dict) or "collector_key" not in meta:
        raise WPLookupError(
            "O WordPress não expõe o meta `collector_key` na REST. "
            "Atualize o tema (inc/dataset-collector.php) antes de publicar."
        )
    return meta


def _all_datasets(api: str, auth) -> list[dict]:
    if api in _all_datasets_cache:
        return _all_datasets_cache[api]
    posts, page = [], 1
    while True:
        r = requests.get(
            api,
            params={
                "status": STATUSES, "context": "edit", "per_page": 100, "page": page,
                "_fields": "id,status,date,title,meta",
            },
            auth=auth, timeout=60,
        )
        if r.status_code != 200:
            raise WPLookupError(f"Falha ao listar datasets ({r.status_code}): {r.text[:200]}")
        posts += r.json()
        if page >= int(r.headers.get("X-WP-TotalPages", 1)):
            break
        page += 1
    _all_datasets_cache[api] = posts
    return posts


def forget_cache() -> None:
    _all_datasets_cache.clear()


def resolve_dataset_post(
    wp_url: str,
    auth: tuple[str, str],
    collector_key: str,
    uf: str | None,
    cached_post_id: int | None = None,
    title: str | None = None,
) -> int | None:
    api = f"{wp_url.rstrip('/')}/wp-json/wp/v2/dataset"
    uf = uf or ""

    def same_collector(meta: dict) -> bool:
        return meta.get("collector_key") == collector_key and meta.get("collector_uf", "") in ("", uf)

    # 1. Meta collector_key
    params = {"collector_key": collector_key, "status": STATUSES, "context": "edit",
              "per_page": 100, "_fields": "id,status,date,title,meta"}
    if uf:
        params["collector_uf"] = uf
    try:
        r = requests.get(api, params=params, auth=auth, timeout=60)
    except requests.RequestException as exc:
        raise WPLookupError(f"Erro de rede ao buscar dataset de {collector_key}: {exc}") from exc
    if r.status_code != 200:
        raise WPLookupError(f"Falha ao buscar dataset de {collector_key} ({r.status_code}): {r.text[:200]}")

    posts = r.json()
    matches = [p for p in posts if same_collector(_meta(p))]
    if posts and not matches:
        # O filtro foi ignorado (tema antigo): a lista veio com posts de outros coletores.
        raise WPLookupError(
            "O filtro `collector_key` da REST foi ignorado. Atualize o tema (inc/dataset-collector.php)."
        )
    if matches:
        chosen = _pick(matches)
        if len(matches) > 1:
            others = [p["id"] for p in matches if p["id"] != chosen["id"]]
            print(f"⚠️ {collector_key}: {len(matches)} posts com o mesmo coletor; usando {chosen['id']} "
                  f"(duplicados: {others})", file=sys.stderr)
        return chosen["id"]

    # 2. wp_post_id em cache (SQLite)
    if cached_post_id:
        try:
            r = requests.get(f"{api}/{cached_post_id}",
                             params={"context": "edit", "_fields": "id,status,meta"}, auth=auth, timeout=30)
        except requests.RequestException as exc:
            raise WPLookupError(f"Erro de rede ao ler o post {cached_post_id}: {exc}") from exc
        if r.status_code == 200:
            meta = _meta(r.json())
            if meta.get("collector_key") in ("", None) or same_collector(meta):
                return cached_post_id
            print(f"⚠️ {collector_key}: o post {cached_post_id} em cache pertence a "
                  f"{meta.get('collector_key')!r}; ignorando o cache.", file=sys.stderr)
        elif r.status_code not in (404, 410):
            raise WPLookupError(f"Falha ao ler o post {cached_post_id} ({r.status_code}): {r.text[:200]}")

    # 3. Título exato (posts criados antes do meta existir)
    if title:
        wanted = _norm_title(title)
        candidates = [
            p for p in _all_datasets(api, auth)
            if _norm_title(_title_raw(p)) == wanted and not _meta(p).get("collector_key")
        ]
        if candidates:
            chosen = _pick(candidates)
            if len(candidates) > 1:
                published = [p for p in candidates if p.get("status") == "publish"]
                if len(published) != 1:
                    ids = [(p["id"], p.get("status")) for p in candidates]
                    raise WPLookupError(
                        f"{collector_key}: {len(candidates)} datasets com o título {title!r} {ids}. "
                        "Remova os duplicados ou preencha o meta collector_key no post certo."
                    )
                chosen = published[0]
                others = [p["id"] for p in candidates if p["id"] != chosen["id"]]
                print(f"⚠️ {collector_key}: usando o post publicado {chosen['id']}; "
                      f"rascunhos duplicados: {others}", file=sys.stderr)
            print(f"🔗 {collector_key}: post {chosen['id']} adotado pelo título.", file=sys.stderr)
            return chosen["id"]

    # 4. Não existe
    return None
