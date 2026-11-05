"""
Referências do IBGE usadas pelos painéis: lista de municípios, população anual e malhas municipais.

As respostas das APIs ficam em cache em `aggregate/ref/` (versionado), para que o cubo seja
reprodutível sem rede. Use `--refresh` para baixar de novo.

    python -m aggregate.ibge refresh --ufs AL BA CE ES MG PE
    python -m aggregate.ibge malhas --ufs AL BA CE ES MG PE --out <tema>/data/malhas
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

import requests

REF_DIR = Path(__file__).resolve().parent / "ref"

UF_CODES = {
    "RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17,
    "MA": 21, "PI": 22, "CE": 23, "RN": 24, "PB": 25, "PE": 26, "AL": 27, "SE": 28, "BA": 29,
    "MG": 31, "ES": 32, "RJ": 33, "SP": 35, "PR": 41, "SC": 42, "RS": 43,
    "MS": 50, "MT": 51, "GO": 52, "DF": 53,
}

SIDRA = "https://apisidra.ibge.gov.br/values"
LOCALIDADES = "https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios"
MALHAS = "https://servicodados.ibge.gov.br/api/v3/malhas/estados/{uf}"

# Estimativas anuais (tabela 6579) + Censos 2010 (1378) e 2022 (4709).
POP_SOURCES = {
    "estimativas": "t/6579/n6/in n3 {ufs}/v/9324/p/all/h/n/f/c",
    "censo2010": "t/1378/n6/in n3 {ufs}/v/93/p/all/c2/0/c287/0/c455/0/h/n/f/c",
    "censo2022": "t/4709/n6/in n3 {ufs}/v/93/p/all/h/n/f/c",
}


def normalize_name(value: str | None) -> str:
    """Nome de município comparável: sem acento, maiúsculo, sem pontuação e espaços repetidos."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    s = s.upper().replace("'", " ").replace("`", " ").replace("-", " ")
    s = re.sub(r"[^A-Z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _get_json(url: str):
    r = requests.get(url, timeout=180, headers={"User-Agent": "observadados-etl"})
    r.raise_for_status()
    return r.json()


def refresh(ufs: list[str]) -> None:
    REF_DIR.mkdir(exist_ok=True)
    municipios = []
    for uf in ufs:
        for m in _get_json(LOCALIDADES.format(uf=uf)):
            municipios.append({"cod": m["id"], "nome": m["nome"], "uf": uf})
    municipios.sort(key=lambda m: m["cod"])
    (REF_DIR / "municipios.json").write_text(
        json.dumps(municipios, ensure_ascii=False, indent=0), encoding="utf-8")

    uf_list = ",".join(str(UF_CODES[u]) for u in ufs)
    pop: dict[str, dict[str, int]] = {}
    origem: dict[str, str] = {}
    for fonte, path in POP_SOURCES.items():
        rows = _get_json(f"{SIDRA}/{path.format(ufs=uf_list)}")
        for row in rows:
            if not str(row.get("V", "")).isdigit():
                continue
            ano = row["D3C"]
            # Censo prevalece sobre estimativa do mesmo ano.
            if ano in origem and origem[ano].startswith("censo") and fonte == "estimativas":
                continue
            pop.setdefault(ano, {})[row["D1C"]] = int(row["V"])
            origem[ano] = fonte
    payload = {
        "fonte": "IBGE/SIDRA: tabelas 6579 (estimativas), 1378 (Censo 2010) e 4709 (Censo 2022)",
        "ufs": ufs,
        "origem_por_ano": dict(sorted(origem.items())),
        "populacao": {a: pop[a] for a in sorted(pop)},
    }
    (REF_DIR / "populacao.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"{len(municipios)} municípios; população para {len(pop)} anos -> {REF_DIR}", file=sys.stderr)


def load_municipios() -> list[dict]:
    return json.loads((REF_DIR / "municipios.json").read_text(encoding="utf-8"))


def load_populacao() -> dict:
    return json.loads((REF_DIR / "populacao.json").read_text(encoding="utf-8"))


def populacao_interpolada(anos: list[int]) -> tuple[dict[int, dict[int, int]], dict[int, str]]:
    """
    {ano: {cod_ibge: pop}} para os anos pedidos. Anos sem dado do IBGE são interpolados
    linearmente entre os anos vizinhos disponíveis (ou repetem o extremo mais próximo).
    Retorna também a origem de cada ano ("estimativas", "censo2010", "interpolado"...).
    """
    ref = load_populacao()
    disp = {int(a): {int(k): v for k, v in m.items()} for a, m in ref["populacao"].items()}
    origem_ref = {int(a): o for a, o in ref["origem_por_ano"].items()}
    known = sorted(disp)
    out, origem = {}, {}
    for ano in anos:
        if ano in disp:
            out[ano], origem[ano] = disp[ano], origem_ref[ano]
            continue
        before = [a for a in known if a < ano]
        after = [a for a in known if a > ano]
        if before and after:
            a0, a1 = before[-1], after[0]
            w = (ano - a0) / (a1 - a0)
            cods = set(disp[a0]) | set(disp[a1])
            out[ano] = {
                c: round(disp[a0].get(c, disp[a1].get(c, 0)) * (1 - w) + disp[a1].get(c, disp[a0].get(c, 0)) * w)
                for c in cods
            }
            origem[ano] = f"interpolado ({a0}–{a1})"
        else:
            nearest = before[-1] if before else after[0]
            out[ano], origem[ano] = disp[nearest], f"repetido de {nearest}"
    return out, origem


def _round_coords(coords, nd: int):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], nd), round(coords[1], nd)]
    return [_round_coords(c, nd) for c in coords]


def malhas(ufs: list[str], out_dir: Path, decimals: int = 3) -> None:
    """Baixa a malha municipal (qualidade mínima) de cada UF e grava GeoJSON compacto."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for uf in ufs:
        url = MALHAS.format(uf=uf) + "?formato=application/vnd.geo%2Bjson&qualidade=minima&intrarregiao=municipio"
        gj = _get_json(url)
        feats = []
        for f in gj["features"]:
            geom = f["geometry"]
            feats.append({
                "type": "Feature",
                "properties": {"cod": int(f["properties"]["codarea"])},
                "geometry": {"type": geom["type"], "coordinates": _round_coords(geom["coordinates"], decimals)},
            })
        path = out_dir / f"{uf}.json"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")),
                        encoding="utf-8")
        print(f"{uf}: {len(feats)} municípios, {path.stat().st_size // 1024} KB", file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh", help="baixa municípios e população para aggregate/ref/")
    r.add_argument("--ufs", nargs="+", required=True)
    m = sub.add_parser("malhas", help="baixa malhas municipais simplificadas")
    m.add_argument("--ufs", nargs="+", required=True)
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--decimals", type=int, default=3)
    a = p.parse_args(argv)
    ufs = [u.upper() for u in a.ufs]
    if a.cmd == "refresh":
        refresh(ufs)
    else:
        malhas(ufs, a.out, a.decimals)


if __name__ == "__main__":
    main()
