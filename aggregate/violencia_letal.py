"""
Cubo agregado do painel de violência letal (AL, BA, CE, ES, MG, PE).

Lê os arquivos mais recentes de cada fonte, harmoniza para
`uf, cod_ibge, ano, mes, natureza, sexo, faixa_etaria, quantidade` e grava um JSON colunar com:

- `municipal_anual`: município × ano × natureza × sexo × faixa
- `uf_mensal`: UF × ano × mês × natureza × sexo × faixa
- população anual por município (IBGE), cobertura por UF e nota metodológica.

Uso:
    python -m aggregate.violencia_letal                       # gera output/paineis/violencia-letal.json
    python -m aggregate.violencia_letal --publish --env .env.local

Os arquivos são procurados em `source-files/<UF>/` e nas pastas passadas em `--search-dir`
(ou na variável OBSERVADADOS_UPLOADS_DIR, apontando para wp-content/uploads/datasets).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from aggregate.ibge import load_municipios, normalize_name, populacao_interpolada

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SLUG = "violencia-letal"
ANO_MINIMO = 2001  # primeiro ano com estimativa de população municipal do IBGE

UFS = ["AL", "BA", "CE", "ES", "MG", "PE"]

NATUREZAS = [
    ("homicidio_doloso", "Homicídio doloso"),
    ("feminicidio", "Feminicídio"),
    ("latrocinio", "Latrocínio (roubo seguido de morte)"),
    ("lcsm", "Lesão corporal seguida de morte"),
    ("outros_cvli", "Outros CVLI (estupro, infanticídio, maus-tratos ou extorsão com morte)"),
    ("miae", "Morte por intervenção de agente do Estado"),
]
NAT_IDX = {n: i for i, (n, _) in enumerate(NATUREZAS)}

INDICADORES = [
    {"id": "homicidio", "label": "Homicídio doloso (inclui feminicídio)",
     "naturezas": ["homicidio_doloso", "feminicidio"]},
    {"id": "cvli", "label": "CVLI: crimes violentos letais intencionais",
     "naturezas": ["homicidio_doloso", "feminicidio", "latrocinio", "lcsm", "outros_cvli"]},
    {"id": "mvi", "label": "MVI: mortes violentas intencionais (CVLI + intervenção de agente do Estado)",
     "naturezas": ["homicidio_doloso", "feminicidio", "latrocinio", "lcsm", "outros_cvli", "miae"]},
]

SEXOS = [("M", "Masculino"), ("F", "Feminino"), ("NI", "Não informado")]
SEXO_IDX = {s: i for i, (s, _) in enumerate(SEXOS)}

FAIXAS = [(0, 14, "0–14"), (15, 19, "15–19"), (20, 24, "20–24"), (25, 29, "25–29"),
          (30, 39, "30–39"), (40, 49, "40–49"), (50, 59, "50–59"), (60, 120, "60+")]
FAIXA_NI = len(FAIXAS)

# Fontes: padrão do nome do arquivo (sem o período YYYYMM) e metadados de cobertura.
FONTES = {
    "AL": {
        "arquivos": {"cvli": r"al-cvli-(\d{6})\.csv"},
        "orgao": "Secretaria de Segurança Pública de Alagoas (SSP/AL)",
        "collector_key": "al_cvli",
        "naturezas": ["homicidio_doloso", "feminicidio", "latrocinio", "lcsm", "outros_cvli"],
        "notas": ["Não inclui mortes por intervenção de agente do Estado."],
    },
    "BA": {
        "arquivos": {f"cvli_{a}": rf"ba-cvli-cvli-bahia-{a}-(\d{{6}})\.csv" for a in range(2023, 2031)},
        "orgao": "Secretaria da Segurança Pública da Bahia (SSP/BA)",
        "collector_key": "ba_cvli",
        "naturezas": [n for n, _ in NATUREZAS],
        "notas": ["Microdados disponíveis apenas a partir de 2023.",
                  "O arquivo de CVLI da Bahia inclui mortes por intervenção de agente do Estado; aqui elas "
                  "são separadas em natureza própria e só entram no indicador MVI."],
    },
    "CE": {
        "arquivos": {"cvli": r"ce-cvli-(\d{6})\.xlsx"},
        "orgao": "Secretaria da Segurança Pública e Defesa Social do Ceará (SSPDS/CE)",
        "collector_key": "ce_cvli",
        "naturezas": ["homicidio_doloso", "feminicidio", "latrocinio", "lcsm", "miae"],
        "notas": ["Mortes por intervenção policial vêm da aba própria da planilha.",
                  "Mortes em unidades prisionais não entram: a aba não informa o município."],
    },
    "ES": {
        "arquivos": {"homicidio": r"es-homicidio-1-(\d{6})\.xlsx", "latrocinio": r"es-latrocinio-(\d{6})\.xlsx"},
        "orgao": "Secretaria de Estado da Segurança Pública e Defesa Social do Espírito Santo (SESP/ES)",
        "collector_key": "es_homicidio",
        "naturezas": ["homicidio_doloso", "feminicidio", "latrocinio"],
        "notas": ["Homicídios e latrocínios vêm de arquivos separados; não há lesão corporal seguida de morte "
                  "nem intervenção de agente do Estado.",
                  "Dados anteriores a 2001 foram omitidos (sem estimativa de população municipal)."],
    },
    "MG": {
        "arquivos": {"homicidio": r"mg-vitima-homicidio-(\d{6})\.csv"},
        "orgao": "Secretaria de Estado de Justiça e Segurança Pública de Minas Gerais (Sejusp/MG)",
        "collector_key": "mg_vitima_homicidio",
        # Feminicídios estão contidos nos homicídios consumados, sem separação.
        "naturezas": ["homicidio_doloso", "feminicidio"],
        "notas": ["Base já agregada por município e mês: só homicídios consumados (feminicídios incluídos, "
                  "sem separação), sem sexo, idade ou outras naturezas. Não entra nos filtros de sexo e "
                  "faixa etária."],
    },
    "PE": {
        "arquivos": {"mvi": r"pe-mortes-violentas-(\d{6})\.xlsx"},
        "orgao": "Secretaria de Defesa Social de Pernambuco (SDS/PE)",
        "collector_key": "pe_mortes_violentas",
        "naturezas": [n for n, _ in NATUREZAS],
        "notas": ["Dados anteriores a 2004 não estão na base de MVI."],
    },
}

# Grafias das fontes que não batem com o nome oficial do IBGE (após normalize_name).
ALIASES = {
    "PE": {"BELEM DE SAO FRANCISCO": "BELEM DO SAO FRANCISCO", "LAGOA DO ITAENGA": "LAGOA DE ITAENGA",
           "ITAMARACA": "ILHA DE ITAMARACA", "SAO CAETANO": "SAO CAITANO", "IGUARACI": "IGUARACY"},
    "CE": {"ITAPAGE": "ITAPAJE"},
    "ES": {},
    "AL": {"OLHO DAGUA DAS FLORES": "OLHO D AGUA DAS FLORES"},
}


# ---------------------------------------------------------------- utilidades

def _strip_accents_upper(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).map(normalize_name)


def classifica_natureza(s: pd.Series) -> pd.Series:
    t = _strip_accents_upper(s)

    def one(v: str) -> str:
        if "INTERVEN" in v or "AGENTE DO ESTADO" in v:
            return "miae"
        if "FEMINIC" in v:
            return "feminicidio"
        if "LATROC" in v or "ROUBO" in v:
            return "latrocinio"
        if v.startswith("LES") and "MORTE" in v:
            return "lcsm"
        if "HOMICID" in v:
            return "homicidio_doloso"
        return "outros_cvli"

    cache = {v: one(v) for v in t.unique()}
    return t.map(cache)


def classifica_sexo(s: pd.Series) -> pd.Series:
    t = _strip_accents_upper(s)
    return t.map(lambda v: "M" if "MASC" in v else ("F" if "FEMI" in v else "NI"))


def classifica_faixa(idade: pd.Series) -> pd.Series:
    n = pd.to_numeric(idade.astype(str).str.extract(r"^\s*(\d{1,3})")[0], errors="coerce")
    out = pd.Series(FAIXA_NI, index=idade.index, dtype="int64")
    for i, (lo, hi, _) in enumerate(FAIXAS):
        out[(n >= lo) & (n <= hi)] = i
    return out


def localizar(uf: str, pattern: str, search_dirs: list[Path]) -> Path:
    """Arquivo mais recente (maior período YYYYMM no nome) entre as pastas de busca."""
    rx = re.compile(pattern, re.IGNORECASE)
    found = []
    for base in search_dirs:
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            m = rx.fullmatch(p.name)
            if m and (p.parent.name.upper() == uf or p.parent.parent.name.upper() == uf):
                found.append((m.group(1), p.stat().st_mtime, p))
    if not found:
        raise FileNotFoundError(f"{uf}: nenhum arquivo para {pattern}")
    return max(found)[2]


def _read_csv(path: Path, **kw) -> pd.DataFrame:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    head = raw[:4000]
    sep = ";" if head.count(b";") > head.count(b",") else ","
    return pd.read_csv(path, sep=sep, encoding=enc, dtype=str, low_memory=False, **kw)


def _frame(uf, municipio, data, natureza, sexo, faixa, qtd=1, cod=None) -> pd.DataFrame:
    data = pd.to_datetime(data, errors="coerce")
    return pd.DataFrame({
        "uf": uf,
        "municipio": municipio if cod is None else None,
        "cod6": cod,
        "ano": data.dt.year,
        "mes": data.dt.month,
        "natureza": natureza,
        "sexo": sexo,
        "faixa": faixa,
        "qtd": qtd,
    })


# ---------------------------------------------------------------- leitores por UF

def ler_al(files):
    df = _read_csv(files["cvli"])
    return [_frame("AL", df["CIDADE DO FATO"], pd.to_datetime(df["DATA DO FATO"], format="%d/%m/%Y", errors="coerce"),
                   classifica_natureza(df["SUBJETIVIDADE COMPLEMENTAR"]), classifica_sexo(df["SEXO DA VITIMA"]),
                   classifica_faixa(df["IDADE DA VITIMA"]))]


def ler_ba(files):
    out = []
    for key, path in files.items():
        df = _read_csv(path)
        qtd = pd.to_numeric(df.get("TOTAL", 1), errors="coerce").fillna(1).astype(int)
        out.append(_frame("BA", None, pd.to_datetime(df["DATA_FATO"], errors="coerce"),
                          classifica_natureza(df["DELITO"]), classifica_sexo(df["SEXO"]),
                          classifica_faixa(df["IDADE"]), qtd=qtd, cod=df["ID_MUNICIPIO"].str.strip().str[:6]))
    return out


def ler_ce(files):
    sheets = pd.read_excel(files["cvli"], sheet_name=None, dtype=str)
    cvli = sheets["CVLI"]
    out = [_frame("CE", cvli["Município"], pd.to_datetime(cvli["Data"], errors="coerce"),
                  classifica_natureza(cvli["Natureza"]), classifica_sexo(cvli["Gênero"]),
                  classifica_faixa(cvli["Idade da Vítima"]))]
    interv = next((v for k, v in sheets.items() if "interven" in normalize_name(k).lower()), None)
    if interv is not None:
        out.append(_frame("CE", interv["Município"], pd.to_datetime(interv["Data"], errors="coerce"),
                          "miae", classifica_sexo(interv["Gênero"]), classifica_faixa(interv["Idade"])))
    return out


def ler_es(files):
    hom = pd.read_excel(files["homicidio"], dtype=str)
    out = [_frame("ES", hom["Municipio"], pd.to_datetime(hom["DataObito"], errors="coerce"),
                  classifica_natureza(hom["TipificacaoJuridico"]), classifica_sexo(hom["Genero"]),
                  classifica_faixa(hom["IdadeEnvolvido"]))]
    lat = pd.read_excel(files["latrocinio"], dtype=str)
    out.append(_frame("ES", lat["Municipio"], pd.to_datetime(lat["Data"], errors="coerce"),
                      "latrocinio", classifica_sexo(lat["GeneroVitima"]), classifica_faixa(lat["IdadeVitima"])))
    return out


def ler_mg(files):
    df = _read_csv(files["homicidio"])
    cols = {normalize_name(c): c for c in df.columns}
    # A coluna de contagem já se chamou "Registros" (202605) e "Vítimas" (202610).
    col_qtd = cols.get("VITIMAS") or cols["REGISTROS"]
    qtd = pd.to_numeric(df[col_qtd], errors="coerce").fillna(0).astype(int)
    data = pd.to_datetime(dict(year=pd.to_numeric(df[cols["ANO FATO"]]), month=pd.to_numeric(df[cols["MES"]]), day=1))
    f = _frame("MG", None, data, "homicidio_doloso", "NI", FAIXA_NI, qtd=qtd,
               cod=df[cols["COD IBGE"]].str.strip().str[:6])
    return [f[f["qtd"] > 0]]


def ler_pe(files):
    df = pd.read_excel(files["mvi"], sheet_name="Plan1", dtype=str)
    data = pd.to_datetime(df["DATA"], errors="coerce")
    qtd = pd.to_numeric(df["TOTAL DE VITIMAS"], errors="coerce").fillna(1).astype(int)
    return [_frame("PE", df["MUNICIPIO"], data, classifica_natureza(df["NATUREZA JURIDICA"]),
                   classifica_sexo(df["SEXO"]), classifica_faixa(df["IDADE"]), qtd=qtd)]


LEITORES = {"AL": ler_al, "BA": ler_ba, "CE": ler_ce, "ES": ler_es, "MG": ler_mg, "PE": ler_pe}


# ---------------------------------------------------------------- montagem

def casar_municipios(df: pd.DataFrame, municipios: list[dict]) -> tuple[pd.DataFrame, list[dict]]:
    """Preenche `cod_ibge` (7 dígitos) a partir do código de 6 dígitos ou do nome normalizado."""
    by6 = {str(m["cod"])[:6]: m["cod"] for m in municipios}
    by_name = {(m["uf"], normalize_name(m["nome"])): m["cod"] for m in municipios}
    cod = df["cod6"].map(by6)
    nomes = df["municipio"].fillna("").astype(str).map(normalize_name)
    chave = [ALIASES.get(u, {}).get(n, n) for u, n in zip(df["uf"], nomes)]
    cod_nome = pd.Series([by_name.get((u, n)) for u, n in zip(df["uf"], chave)], index=df.index)
    df["cod_ibge"] = cod.fillna(cod_nome)
    sem = df[df["cod_ibge"].isna()]
    perdas = (sem.assign(origem=sem["municipio"].fillna(sem["cod6"]).fillna("(vazio)"))
              .groupby(["uf", "origem"])["qtd"].sum().reset_index()
              .sort_values("qtd", ascending=False))
    return df, [{"uf": r.uf, "valor": r.origem, "vitimas": int(r.qtd)} for r in perdas.itertuples()]


def colunar(df: pd.DataFrame, cols: list[str], ano0: int, delta: str) -> dict:
    """
    JSON colunar compacto. `ano` vem como deslocamento de `ano0` (dims.anos[0]) e a coluna `delta`
    vem codificada por diferenças (o valor real é a soma acumulada), já que as linhas estão ordenadas por ela.
    """
    df = df.sort_values(cols).reset_index(drop=True)
    data = {c: df[c].astype(int) for c in cols + ["n"]}
    data["ano"] = data["ano"] - ano0
    data[delta] = data[delta].diff().fillna(data[delta].iloc[0] if len(df) else 0)
    return {"cols": cols + ["n"], "rows": len(df), "ano_offset": True, "delta": [delta],
            "data": {c: v.astype(int).tolist() for c, v in data.items()}}


def montar(search_dirs: list[Path]) -> dict:
    municipios = load_municipios()
    frames, arquivos_usados = [], {}
    for uf in UFS:
        cfg = FONTES[uf]
        files = {}
        for key, pat in cfg["arquivos"].items():
            try:
                files[key] = localizar(uf, pat, search_dirs)
            except FileNotFoundError:
                if uf == "BA":  # anos futuros da BA ainda não existem
                    continue
                raise
        arquivos_usados[uf] = sorted(p.name for p in files.values())
        print(f"{uf}: {', '.join(arquivos_usados[uf])}", file=sys.stderr)
        frames.extend(LEITORES[uf](files))

    df = pd.concat(frames, ignore_index=True)
    sem_data = df[df["ano"].isna()].groupby("uf")["qtd"].sum().to_dict()
    df = df[df["ano"].notna()].copy()
    df["ano"] = df["ano"].astype(int)
    df["mes"] = df["mes"].astype(int)
    antes = df[df["ano"] < ANO_MINIMO].groupby("uf")["qtd"].sum().to_dict()
    df = df[df["ano"] >= ANO_MINIMO]

    df, nao_casados = casar_municipios(df, municipios)
    df["nat"] = df["natureza"].map(NAT_IDX)
    df["sx"] = df["sexo"].map(SEXO_IDX)
    df["uf_i"] = df["uf"].map({u: i for i, u in enumerate(UFS)})

    mun_cods = [m["cod"] for m in municipios if m["uf"] in UFS]
    mun_idx = {c: i for i, c in enumerate(mun_cods)}
    dm = df[df["cod_ibge"].notna()].copy()
    dm["mun"] = dm["cod_ibge"].astype(int).map(mun_idx)

    municipal = (dm.groupby(["mun", "ano", "nat", "sx", "faixa"])["qtd"].sum()
                 .reset_index().rename(columns={"qtd": "n"}))
    mensal = (df.groupby(["uf_i", "ano", "mes", "nat", "sx", "faixa"])["qtd"].sum()
              .reset_index().rename(columns={"qtd": "n", "uf_i": "uf"}))

    anos = list(range(int(df["ano"].min()), int(df["ano"].max()) + 1))
    pop, pop_origem = populacao_interpolada(anos)

    cobertura = {}
    for uf in UFS:
        d = df[df["uf"] == uf]
        ult = d.loc[d["ano"].idxmax()] if len(d) else None
        ini = d[d["ano"] == d["ano"].min()]["mes"].min()
        fim_ano = int(d["ano"].max())
        fim_mes = int(d[d["ano"] == fim_ano]["mes"].max())
        tot = int(d["qtd"].sum())
        cobertura[uf] = {
            "inicio": f"{int(d['ano'].min())}-{int(ini):02d}",
            "fim": f"{fim_ano}-{fim_mes:02d}",
            "naturezas": FONTES[uf]["naturezas"],
            "sexo_idade": uf != "MG",
            "vitimas": tot,
            "sem_municipio": int(sum(x["vitimas"] for x in nao_casados if x["uf"] == uf)),
            "sem_data": int(sem_data.get(uf, 0)),
            "antes_de_2001": int(antes.get(uf, 0)),
            "orgao": FONTES[uf]["orgao"],
            "collector_key": FONTES[uf]["collector_key"],
            "arquivos": arquivos_usados[uf],
            "notas": FONTES[uf]["notas"],
        }

    tz = timezone(timedelta(hours=-3))
    return {
        "schema": "observadados/painel-cubo",
        "schema_version": 1,
        "painel": SLUG,
        "titulo": "Violência letal",
        "generated_at": datetime.now(tz).isoformat(timespec="seconds"),
        "generator": "observadados-etl/aggregate.violencia_letal 1.0",
        "dims": {
            "uf": UFS,
            # `residual`: categoria pequena cuja ausência numa UF não torna a cobertura parcial.
            "natureza": [{"id": n, "label": l, **({"residual": True} if n == "outros_cvli" else {})}
                         for n, l in NATUREZAS],
            "sexo": [{"id": s, "label": l} for s, l in SEXOS],
            "faixa": [l for _, _, l in FAIXAS] + ["Não informada"],
            "anos": anos,
        },
        "indicadores": [{**i, "naturezas": [NAT_IDX[n] for n in i["naturezas"]]} for i in INDICADORES],
        "cobertura": cobertura,
        "municipios": {
            "cod": mun_cods,
            "nome": [m["nome"] for m in municipios if m["uf"] in UFS],
            "uf": [UFS.index(m["uf"]) for m in municipios if m["uf"] in UFS],
        },
        "populacao": {
            "fonte": "IBGE: estimativas anuais (SIDRA 6579), Censos 2010 e 2022; anos sem dado interpolados.",
            "origem_por_ano": {str(a): o for a, o in pop_origem.items()},
            # valores[i_ano][i_municipio]; 0 quando o município não existia
            "valores": [[pop[a].get(c, 0) for c in mun_cods] for a in anos],
        },
        "municipal_anual": colunar(municipal, ["mun", "ano", "nat", "sx", "faixa"], anos[0], "mun"),
        "uf_mensal": colunar(mensal, ["uf", "ano", "mes", "nat", "sx", "faixa"], anos[0], "uf"),
        "nao_casados": nao_casados,
    }


# ---------------------------------------------------------------- publicação

def publicar(cubo_path: Path, env_file: str) -> None:
    import requests
    from core.wp_client import _load_env

    loaded = _load_env(env_file, override=True)
    if loaded is None:
        sys.exit(f"Arquivo de ambiente não encontrado: {env_file}")
    wp_url, user, pw = (os.environ.get(k) for k in ("WP_URL", "WP_USER", "WP_APP_PASSWORD"))
    if not (wp_url and user and pw):
        sys.exit("WP_URL/WP_USER/WP_APP_PASSWORD ausentes")
    if "observadados.org" in wp_url and env_file != ".env.production":
        sys.exit(f"Recusado: {env_file} aponta para produção ({wp_url}).")
    url = f"{wp_url.rstrip('/')}/wp-json/observadados/v1/painel/{SLUG}/cubo"
    r = requests.post(url, data=cubo_path.read_bytes(), auth=(user, pw), timeout=120,
                      headers={"Content-Type": "application/json"})
    if r.status_code >= 300:
        sys.exit(f"Falha ao publicar ({r.status_code}): {r.text[:500]}")
    print(f"Publicado em {wp_url}: {r.json()}", file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--search-dir", action="append", type=Path, default=[],
                   help="pasta adicional com os arquivos por UF (pode repetir)")
    p.add_argument("--out", type=Path, default=PROJECT_ROOT / "output" / "paineis" / f"{SLUG}.json")
    p.add_argument("--publish", action="store_true", help="envia o cubo ao WordPress")
    p.add_argument("--env", default=".env.local", help="arquivo de ambiente para --publish")
    a = p.parse_args(argv)

    dirs = [PROJECT_ROOT / "source-files", *a.search_dir]
    if os.environ.get("OBSERVADADOS_UPLOADS_DIR"):
        dirs.append(Path(os.environ["OBSERVADADOS_UPLOADS_DIR"]))

    cubo = montar(dirs)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(cubo, ensure_ascii=False, separators=(",", ":"))
    a.out.write_text(body, encoding="utf-8")
    gz = len(gzip.compress(body.encode("utf-8")))
    print(f"Cubo: {a.out} ({len(body) // 1024} KB, {gz // 1024} KB gzip); "
          f"municipal_anual={cubo['municipal_anual']['rows']} linhas, uf_mensal={cubo['uf_mensal']['rows']} linhas",
          file=sys.stderr)
    if cubo["nao_casados"]:
        print("Municípios não casados:", file=sys.stderr)
        for x in cubo["nao_casados"][:40]:
            print(f"  {x['uf']} {x['valor']!r}: {x['vitimas']}", file=sys.stderr)
    if a.publish:
        publicar(a.out, a.env)


if __name__ == "__main__":
    main()
