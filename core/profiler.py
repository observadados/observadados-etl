"""
Perfil de arquivos tabulares (CSV / XLSX / XLS) no formato `metadados` schema v2.

O contrato do JSON está em docs/profile-schema.md (compartilhado com o tema
WordPress). As estatísticas são calculadas sobre o arquivo inteiro, lido em
blocos (chunks) com pandas, para que o uso de memória fique limitado mesmo
para CSVs de dezenas de MB.

Uso:
    from core.profiler import profile_file, build_profile
    json_str = profile_file(Path("arquivo.csv"))   # string JSON ou "{}" em caso de erro
    perfil = build_profile(Path("arquivo.csv"))    # dict (levanta exceção em caso de erro)

Linha de comando (útil para testes):
    python -m core.profiler arquivo.csv [-o saida.json]
"""

from __future__ import annotations

import codecs
import csv
import io
import itertools
import json
import math
import re
import sys
import warnings
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

SCHEMA_VERSION = 2
GENERATOR = "observadados-etl/profiler 2.0"

CHUNK_ROWS = 200_000          # linhas por bloco de leitura
SAMPLE_ROWS = 100             # linhas em `sample`
SAMPLE_MAX_CHARS = 200        # truncamento das strings em `sample`
TOP_N = 10                    # valores em `top`
MAX_JSON_BYTES = 200 * 1024   # limite do contrato
COUNTER_CAP = 100_000         # distintos contados exatamente por coluna
COUNTER_KEEP = 20_000         # distintos mantidos após estourar o limite (top aproximado)
KMV_K = 4096                  # tamanho do sketch para estimar distintos
MISSING_WARN_PCT = 20.0       # aviso de ausentes a partir deste percentual
PARSE_TOLERANCE = 0.01        # fração máxima de valores não reconhecidos em coluna numérica/data

MISSING_TOKENS = frozenset(
    ["", "NULL", "null", "Null", "NaN", "nan", "NA", "N/A", "n/a", "#N/D", "#N/A", "None"]
)
TRUE_TOKENS = frozenset(["sim", "s", "true", "verdadeiro", "yes", "y", "t"])
FALSE_TOKENS = frozenset(["não", "nao", "n", "false", "falso", "no", "f"])

_INT_RE = re.compile(r"^[-+]?\d+$")
_PLAIN_NUM_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?$")
_BR_NUM_RE = re.compile(r"^[-+]?(?:\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+,\d+)$")
_BR_THOUSANDS_RE = re.compile(r"^[-+]?[1-9]\d{0,2}(?:\.\d{3})+$")
_BR_MULTIDOT_RE = re.compile(r"^[-+]?\d{1,3}(?:\.\d{3}){2,}(?:,\d+)?$")
_DATE_RE = re.compile(
    r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_DATE_FORMATS = [
    "%d/%m/%Y",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "ISO8601",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%d/%m/%y",
]


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _num(x, digits: int = 4):
    """Converte para número JSON (int quando inteiro), sem NaN/inf."""
    if x is None:
        return None
    x = float(x)
    if not math.isfinite(x):
        return None
    x = round(x, digits)
    if x.is_integer() and abs(x) < 2**53:
        return int(x)
    return x


def _pct(part: float, total: float) -> float:
    if not total:
        return 0
    return _num(100.0 * part / total, 2)


def _br_pct(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def detect_encoding(path: Path) -> tuple[str, str]:
    """
    Retorna (nome_exibido, nome_para_leitura). Valida o arquivo inteiro em UTF-8
    (streaming); se falhar, usa cp1252, e latin-1 como último recurso.
    """
    with open(path, "rb") as f:
        head = f.read(4)
        bom = head.startswith(codecs.BOM_UTF8)
        f.seek(0)
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            while True:
                block = f.read(8 * 1024 * 1024)
                if not block:
                    decoder.decode(b"", final=True)
                    break
                decoder.decode(block)
            return "utf-8", ("utf-8-sig" if bom else "utf-8")
        except UnicodeDecodeError:
            pass
    # cp1252 não define 0x81, 0x8D, 0x8F, 0x90, 0x9D
    with open(path, "rb") as f:
        try:
            while True:
                block = f.read(8 * 1024 * 1024)
                if not block:
                    break
                block.decode("cp1252")
            return "cp1252", "cp1252"
        except UnicodeDecodeError:
            return "latin-1", "latin-1"


def detect_delimiter(text: str) -> str:
    candidates = [";", ",", "\t", "|"]
    lines = [ln for ln in text.splitlines() if ln.strip()][:50]
    if not lines:
        return ","
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines[:20]), delimiters="".join(candidates))
        counts = {
            len(row) for row in csv.reader(io.StringIO("\n".join(lines[:20])), delimiter=dialect.delimiter)
        }
        if dialect.delimiter in candidates and min(counts) >= 2 and len(counts) <= 2:
            return dialect.delimiter
    except csv.Error:
        pass
    header = lines[0]
    return max(candidates, key=lambda c: header.count(c)) if any(c in header for c in candidates) else ","


def _parse_dates(s: pd.Series, fmts: list[str]) -> pd.Series:
    """Converte strings em datetime tentando os formatos em ordem (NaT se nenhum servir)."""
    out = None
    for fmt in fmts:
        todo = s if out is None else s[out.isna()]
        if todo.empty:
            break
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(todo, format=fmt, errors="coerce", utc=(fmt == "ISO8601"))
        if getattr(parsed.dt, "tz", None) is not None:
            parsed = parsed.dt.tz_localize(None)
        out = parsed if out is None else out.fillna(parsed)
    if out is None:
        return pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    return out


def _cell_to_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        if v.time() == time(0, 0):
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, time):
        return v.strftime("%H:%M:%S")
    if isinstance(v, float):
        if math.isfinite(v) and v.is_integer() and abs(v) < 2**53:
            return str(int(v))
        return repr(v)
    return str(v)


def _unique_names(raw: list) -> list[str]:
    names, seen = [], Counter()
    for i, n in enumerate(raw):
        n = "" if n is None else str(n).strip()
        if not n:
            n = f"Unnamed: {i}"
        seen[n] += 1
        names.append(n if seen[n] == 1 else f"{n}.{seen[n] - 1}")
    return names


# --------------------------------------------------------------------------- #
# Acumulador por coluna
# --------------------------------------------------------------------------- #
class _Column:
    def __init__(self, name: str, position: int):
        self.name = name
        self.position = position
        self.missing = 0
        self.nonnull = 0
        self.vc: pd.Series | None = None
        self.overflow = False
        self.configured = False
        self.kmv = np.empty(0, dtype=np.uint64)
        self.len_min = None
        self.len_max = None
        self.len_sum = 0
        # numérico
        self.num_style: str | None = None   # "br" | "us" | None
        self.num_fail = 0
        self.num_parts: list[np.ndarray] = []
        # data
        self.date_fmt: list[str] | None = None
        self.date_fail = 0
        self.date_min = None
        self.date_max = None
        self.date_has_time = False
        self.months: Counter = Counter()

    # -- configuração a partir do primeiro bloco -------------------------------
    def configure(self, vc: pd.Series, delimiter: str | None) -> None:
        if vc.empty:
            return
        vals = vc.index.to_numpy(dtype=object)
        cnts = vc.to_numpy()
        total = cnts.sum()

        def frac(mask) -> float:
            return cnts[mask].sum() / total if total else 0.0

        is_plain = np.fromiter((bool(_PLAIN_NUM_RE.match(v)) for v in vals), bool, len(vals))
        is_br = np.fromiter((bool(_BR_NUM_RE.match(v)) for v in vals), bool, len(vals))
        is_int = np.fromiter((bool(_INT_RE.match(v)) for v in vals), bool, len(vals))

        has_comma = any(is_br[i] and "," in vals[i] for i in range(len(vals)))
        has_multidot = any(_BR_MULTIDOT_RE.match(v) for v in vals)
        dotted = [v for v, p in zip(vals, is_plain) if p and "." in v]
        all_thousands = bool(dotted) and all(_BR_THOUSANDS_RE.match(v) for v in dotted)

        if has_comma or has_multidot or (all_thousands and delimiter == ";"):
            if frac(is_br | is_int) >= 1 - PARSE_TOLERANCE:
                self.num_style = "br"
        elif frac(is_plain) >= 1 - PARSE_TOLERANCE:
            self.num_style = "us"
        if self.num_style:
            return

        is_date = np.fromiter((bool(_DATE_RE.match(v)) for v in vals), bool, len(vals))
        if frac(is_date) < 1 - PARSE_TOLERANCE:
            return
        probe = pd.Series(vals[is_date][:20000])
        probe_cnt = cnts[is_date][:20000]
        weight = probe_cnt.sum()
        # escolhe gulosamente até 2 formatos (ex.: "dd/mm/aaaa hh:mm" misturado com "dd/mm/aaaa")
        chosen: list[str] = []
        ok = np.zeros(len(probe), dtype=bool)
        for _ in range(2):
            best, best_ok = None, ok
            for fmt in _DATE_FORMATS:
                if fmt in chosen:
                    continue
                parsed = _parse_dates(probe[~ok], [fmt])
                cand = ok.copy()
                cand[~ok] = parsed.notna().to_numpy()
                if probe_cnt[cand].sum() > probe_cnt[best_ok].sum():
                    best, best_ok = fmt, cand
            if best is None:
                break
            chosen.append(best)
            ok = best_ok
            if ok.all():
                break
        if chosen and probe_cnt[ok].sum() / weight >= 1 - PARSE_TOLERANCE:
            self.date_fmt = chosen

    # -- atualização por bloco -------------------------------------------------
    def update(self, nn: pd.Series, n_missing: int, vc: pd.Series) -> None:
        self.missing += n_missing
        n = len(nn)
        self.nonnull += n
        if n == 0:
            return

        # contagem de valores (exata até COUNTER_CAP distintos)
        self.vc = vc if self.vc is None else self.vc.add(vc, fill_value=0)
        if len(self.vc) > COUNTER_CAP:
            self.overflow = True
            self.vc = self.vc.nlargest(COUNTER_KEEP)

        # sketch KMV para distintos
        h = pd.util.hash_array(vc.index.to_numpy(dtype=object))
        self.kmv = np.unique(np.concatenate([self.kmv, h]))[:KMV_K]

        lens = nn.str.len()
        lmin, lmax = int(lens.min()), int(lens.max())
        self.len_min = lmin if self.len_min is None else min(self.len_min, lmin)
        self.len_max = lmax if self.len_max is None else max(self.len_max, lmax)
        self.len_sum += int(lens.sum())

        if self.num_style:
            s = nn
            if self.num_style == "br":
                s = s.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
            parsed = pd.to_numeric(s, errors="coerce")
            ok = parsed.notna()
            self.num_fail += int(n - ok.sum())
            self.num_parts.append(parsed[ok].to_numpy(dtype=np.float64))
            if self.num_fail > PARSE_TOLERANCE * self.nonnull and self.nonnull > 1000:
                self.num_style, self.num_parts = None, []
        elif self.date_fmt:
            dt = _parse_dates(nn, self.date_fmt).dropna()
            self.date_fail += int(n - len(dt))
            if len(dt):
                dmin, dmax = dt.min(), dt.max()
                self.date_min = dmin if self.date_min is None else min(self.date_min, dmin)
                self.date_max = dmax if self.date_max is None else max(self.date_max, dmax)
                if not self.date_has_time and (dt != dt.dt.normalize()).any():
                    self.date_has_time = True
                keys = (dt.dt.year * 12 + dt.dt.month - 1).to_numpy()
                uk, uc = np.unique(keys, return_counts=True)
                self.months.update(dict(zip(uk.tolist(), uc.tolist())))
            if self.date_fail > PARSE_TOLERANCE * self.nonnull and self.nonnull > 1000:
                self.date_fmt = None
                self.months = Counter()

    # -- resultado -------------------------------------------------------------
    def unique(self) -> tuple[int, bool]:
        if self.vc is None:
            return 0, False
        if not self.overflow:
            return int(len(self.vc)), False
        if len(self.kmv) < KMV_K:
            return int(len(self.kmv)), True
        est = (KMV_K - 1) * (2.0**64) / float(self.kmv[-1])
        return int(min(round(est), self.nonnull)), True

    def to_field(self, total_rows: int, warn: list[str]) -> dict:
        unique, unique_est = self.unique()
        field = {
            "name": self.name,
            "position": self.position,
            "type": "empty",
            "missing": int(self.missing),
            "missing_pct": _pct(self.missing, total_rows),
            "unique": unique,
            "unique_is_estimate": unique_est,
            "description": None,
        }
        if self.nonnull == 0:
            warn.append(f"Coluna {self.name} está totalmente vazia")
            return field

        if field["missing_pct"] >= MISSING_WARN_PCT:
            warn.append(f"Coluna {self.name} tem {_br_pct(field['missing_pct'])}% de valores ausentes")
        if unique == 1 and total_rows > 1:
            warn.append(f"Coluna {self.name} tem um único valor")

        if self.num_style and self.num_fail <= PARSE_TOLERANCE * self.nonnull and self.num_parts:
            arr = np.concatenate(self.num_parts)
            self.num_parts = []
            if len(arr):
                is_int = bool(np.all(np.mod(arr, 1) == 0))
                field["type"] = "integer" if is_int else "numeric"
                field.update(self._numeric(arr, is_int))
                if self.num_fail:
                    warn.append(
                        f"Coluna {self.name}: {self.num_fail} valores não numéricos foram ignorados nas estatísticas"
                    )
                return field

        if self.date_fmt and self.date_fail <= PARSE_TOLERANCE * self.nonnull and self.months:
            field["type"] = "datetime" if self.date_has_time else "date"
            field.update(self._dates())
            if self.date_fail:
                warn.append(
                    f"Coluna {self.name}: {self.date_fail} valores não reconhecidos como data foram ignorados"
                )
            return field

        lowered = {str(v).lower() for v in self.vc.index} if not self.overflow else set()
        is_bool = (
            len(lowered) == 2
            and lowered <= (TRUE_TOKENS | FALSE_TOKENS)
            and lowered & TRUE_TOKENS
            and lowered & FALSE_TOKENS
        )
        if is_bool:
            field["type"] = "boolean"
        elif unique <= 50 or unique <= 0.05 * self.nonnull:
            field["type"] = "categorical"
        else:
            field["type"] = "text"
        field.update(self._top())
        if field["type"] == "text":
            field["length"] = {
                "min": int(self.len_min),
                "max": int(self.len_max),
                "mean": _num(self.len_sum / self.nonnull, 2),
            }
        if self.overflow:
            pass  # top com contagens aproximadas; `unique_is_estimate` já sinaliza
        return field

    def _numeric(self, arr: np.ndarray, is_int: bool) -> dict:
        q1, med, q3 = np.percentile(arr, [25, 50, 75])
        stats = {
            "min": _num(arr.min()),
            "max": _num(arr.max()),
            "mean": _num(arr.mean()),
            "median": _num(med),
            "std": _num(arr.std(ddof=1)) if len(arr) > 1 else 0,
            "q1": _num(q1),
            "q3": _num(q3),
        }
        lo, hi = float(arr.min()), float(arr.max())
        if is_int and hi - lo < 20:
            edges = np.arange(lo, hi + 2)  # um intervalo por valor inteiro
            counts, edges = np.histogram(arr, bins=edges)
        elif lo == hi:
            counts, edges = np.array([len(arr)]), np.array([lo, hi])
        else:
            nbins = int(min(20, max(10, math.ceil(math.log2(len(arr)) + 1))))
            counts, edges = np.histogram(arr, bins=nbins)
        return {
            "stats": stats,
            "histogram": {
                "bins": [_num(e) for e in edges],
                "counts": [int(c) for c in counts],
            },
        }

    def _dates(self) -> dict:
        fmt = "%Y-%m-%dT%H:%M:%S" if self.date_has_time else "%Y-%m-%d"
        kmin, kmax = min(self.months), max(self.months)
        span = kmax - kmin + 1
        if span <= 36:
            keys = range(kmin, kmax + 1)
            labels = [f"{k // 12:04d}-{k % 12 + 1:02d}" for k in keys]
            counts = [int(self.months.get(k, 0)) for k in keys]
            gran = "month"
        else:
            years: Counter = Counter()
            for k, c in self.months.items():
                years[k // 12] += c
            ys = range(min(years), max(years) + 1)
            labels = [f"{y:04d}" for y in ys]
            counts = [int(years.get(y, 0)) for y in ys]
            gran = "year"
        return {
            "range": {"min": self.date_min.strftime(fmt), "max": self.date_max.strftime(fmt)},
            "timeline": {"granularity": gran, "labels": labels, "counts": counts},
        }

    def _top(self) -> dict:
        top_vc = self.vc.nlargest(TOP_N, keep="first")
        # desempate estável: contagem desc, valor asc
        items = sorted(((str(k), int(v)) for k, v in top_vc.items()), key=lambda kv: (-kv[1], kv[0]))
        top = [{"value": k, "count": c, "pct": _pct(c, self.nonnull)} for k, c in items]
        covered = sum(c for _, c in items)
        other = max(0.0, 100.0 * (self.nonnull - covered) / self.nonnull)
        return {"top": top, "other_pct": _num(other, 2)}


# --------------------------------------------------------------------------- #
# Leitura em blocos
# --------------------------------------------------------------------------- #
def _iter_csv(path: Path, encoding: str, delimiter: str, bad_lines: list) -> tuple[list[str], Iterator[pd.DataFrame]]:
    with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
        header = next(csv.reader(f, delimiter=delimiter), [])
    names = _unique_names(header)

    def gen():
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            reader = pd.read_csv(
                path,
                sep=delimiter,
                encoding=encoding,
                encoding_errors="replace",
                header=None,
                skiprows=1,
                names=list(range(len(names))),
                dtype=str,
                na_filter=False,
                keep_default_na=False,
                chunksize=CHUNK_ROWS,
                engine="c",
                on_bad_lines="warn",
                skip_blank_lines=True,
            )
            for chunk in reader:
                for w in caught:
                    bad_lines.extend(re.findall(r"Skipping line \d+", str(w.message)))
                caught.clear()
                yield chunk.fillna("")
            for w in caught:
                bad_lines.extend(re.findall(r"Skipping line \d+", str(w.message)))

    return names, gen()


def _iter_xlsx(path: Path) -> tuple[list[str], Iterator[pd.DataFrame], str]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    # Planilhas de órgãos costumam ter abas de metodologia/tabelas dinâmicas: escolhe a
    # aba com maior área declarada (linhas x colunas) como a tabela de dados.
    ws = max(wb.worksheets, key=lambda w: (w.max_row or 0) * (w.max_column or 0))
    rows = ws.iter_rows(values_only=True)
    # cabeçalho: primeira linha (entre as 30 primeiras) com pelo menos metade das
    # células preenchidas da linha mais cheia
    head_rows = []
    for r in rows:
        head_rows.append(list(r))
        if len(head_rows) >= 30:
            break
    filled = [sum(1 for v in r if v is not None and str(v).strip() != "") for r in head_rows]
    best = max(filled, default=0)
    hidx = next((i for i, n in enumerate(filled) if n >= max(2, best / 2)), 0) if best else 0
    header = head_rows[hidx] if head_rows else []
    pending = head_rows[hidx + 1 :]
    while header and (header[-1] is None or str(header[-1]).strip() == ""):
        header.pop()
    names = _unique_names(header)
    ncols = len(names)

    def gen():
        buf = []
        try:
            for r in itertools.chain(pending, rows):
                vals = [_cell_to_str(v) for v in r[:ncols]]
                if len(vals) < ncols:
                    vals += [""] * (ncols - len(vals))
                if not any(vals):
                    continue
                buf.append(vals)
                if len(buf) >= CHUNK_ROWS // 4:
                    yield pd.DataFrame(buf, columns=list(range(ncols)), dtype=object)
                    buf = []
            if buf:
                yield pd.DataFrame(buf, columns=list(range(ncols)), dtype=object)
        finally:
            wb.close()

    return names, gen(), ws.title


def _iter_xls(path: Path) -> tuple[list[str], Iterator[pd.DataFrame], str]:
    # Requer xlrd; lê tudo em memória (arquivos .xls são limitados a 65.536 linhas).
    xl = pd.ExcelFile(path)
    sheet = xl.sheet_names[0]
    df = xl.parse(sheet, header=None, dtype=object)
    header = df.iloc[0].tolist() if len(df) else []
    names = _unique_names(header)
    body = df.iloc[1:, : len(names)].map(lambda v: "" if pd.isna(v) else _cell_to_str(v))
    body.columns = list(range(len(names)))
    return names, iter([body]), sheet


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def build_profile(file_path: Path, display_name: str | None = None) -> dict:
    """Gera o perfil v2 (dict). Levanta exceção se o arquivo não puder ser lido."""
    file_path = Path(file_path)
    ext = file_path.suffix.lower()
    warn: list[str] = []
    bad_lines: list[str] = []
    file_info = {
        "name": display_name or file_path.name,
        "format": ext.lstrip("."),
        "size_bytes": file_path.stat().st_size,
        "encoding": None,
        "delimiter": None,
        "sheet": None,
    }

    if ext in (".csv", ".txt"):
        file_info["format"] = "csv"
        enc_name, enc_read = detect_encoding(file_path)
        with open(file_path, "r", encoding=enc_read, errors="replace", newline="") as f:
            head_text = f.read(64 * 1024)
        delimiter = detect_delimiter(head_text)
        file_info["encoding"] = enc_name
        file_info["delimiter"] = delimiter
        names, chunks = _iter_csv(file_path, enc_read, delimiter, bad_lines)
    elif ext == ".xlsx":
        delimiter = None
        names, chunks, sheet = _iter_xlsx(file_path)
        file_info["sheet"] = sheet
    elif ext == ".xls":
        delimiter = None
        names, chunks, sheet = _iter_xls(file_path)
        file_info["sheet"] = sheet
    else:
        raise ValueError(f"Formato não suportado para perfil: {ext}")

    cols = [_Column(n, i) for i, n in enumerate(names)]
    total_rows = 0
    sample_rows: list[list] = []
    first = True

    for chunk in chunks:
        total_rows += len(chunk)
        stripped = {}
        for i, col in enumerate(cols):
            s = chunk[i].astype(str).str.strip()
            miss = s.isin(MISSING_TOKENS)
            stripped[i] = (s, miss)
        if first:
            head = min(SAMPLE_ROWS, len(chunk))
            for r in range(head):
                row = []
                for i in range(len(cols)):
                    s, miss = stripped[i]
                    row.append(None if miss.iat[r] else s.iat[r][:SAMPLE_MAX_CHARS])
                sample_rows.append(row)
        for i, col in enumerate(cols):
            s, miss = stripped[i]
            nn = s[~miss]
            vc = nn.value_counts(sort=False)
            if not col.configured and len(vc):
                col.configure(vc, delimiter)
                col.configured = True
            col.update(nn, int(miss.sum()), vc)
        first = False

    fields = [c.to_field(total_rows, warn) for c in cols]
    if bad_lines:
        warn.insert(0, f"{len(bad_lines)} linhas malformadas (número de colunas incorreto) foram ignoradas")

    tz = None
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Sao_Paulo")
    except Exception:
        pass
    now = datetime.now(tz) if tz else datetime.now().astimezone()

    profile = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "generator": GENERATOR,
        "file": file_info,
        "rows": total_rows,
        "rows_exact": True,
        "columns": len(cols),
        "profiled_rows": total_rows,
        "sampled": False,
        "sample": {"columns": names, "rows": sample_rows},
        "fields": fields,
        "warnings": warn,
    }
    _fit_size(profile)
    return profile


def _dumps(profile: dict) -> str:
    return json.dumps(profile, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _fit_size(profile: dict) -> None:
    """Reduz amostra/top até o JSON caber em MAX_JSON_BYTES."""
    def size() -> int:
        return len(_dumps(profile).encode("utf-8"))

    if size() <= MAX_JSON_BYTES:
        return
    rows = profile["sample"]["rows"]
    for n in (50, 20, 10, 0):
        profile["sample"]["rows"] = rows[:n]
        if size() <= MAX_JSON_BYTES:
            profile["warnings"].append(f"Amostra reduzida para {n} linhas para respeitar o limite de tamanho")
            return
    # Arquivos muito largos: encurta `top` em etapas (valores truncados e menos itens).
    original = {id(f): [dict(t) for t in f["top"]] for f in profile["fields"] if "top" in f}
    for top_n, max_len in ((10, 60), (5, 60), (5, 30), (3, 30), (1, 20)):
        for f in profile["fields"]:
            if "top" not in f:
                continue
            f["top"] = [dict(t, value=t["value"][:max_len]) for t in original[id(f)][:top_n]]
            f["other_pct"] = _num(max(0.0, 100.0 - sum(t["pct"] for t in f["top"])), 2)
        if size() <= MAX_JSON_BYTES:
            break
    profile["warnings"].append("Perfil resumido (amostra e valores mais frequentes) para respeitar o limite de tamanho")


def profile_file(file_path: Path, display_name: str | None = None) -> str:
    """
    Gera o perfil v2 e devolve a string JSON a gravar no subcampo ACF `metadados`.
    Em caso de erro, devolve "{}" (o tema trata JSON vazio como "sem perfil").
    """
    try:
        return _dumps(build_profile(Path(file_path), display_name))
    except Exception as e:
        print(f"⚠️ Erro ao gerar perfil de {Path(file_path).name}: {e}", file=sys.stderr)
        return "{}"


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Gera o perfil v2 (metadados) de um arquivo.")
    p.add_argument("file")
    p.add_argument("-o", "--output")
    a = p.parse_args()
    prof = build_profile(Path(a.file))
    out = json.dumps(prof, ensure_ascii=False, indent=2, allow_nan=False)
    if a.output:
        Path(a.output).write_text(out, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(out)
