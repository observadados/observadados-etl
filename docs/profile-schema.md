# Contrato do perfil de arquivo (`metadados`, schema v2)

Documento compartilhado entre **observadados-etl** (produz) e **tema-observadados** (consome).
Existe uma cópia idêntica em cada repositório; qualquer mudança deve ser feita nas duas.

## Transporte

- O perfil é gravado no subcampo ACF **`metadados`** de cada linha do repeater **`arquivos`** do post `dataset` (o mesmo campo usado hoje pelo v1).
- Conteúdo: uma string JSON (UTF-8, `ensure_ascii=False`), no máximo **200 KB**.
- O ETL **deixa de anexar HTML de metadados** ao subcampo `descricao` quando gera v2. Posts antigos ainda têm esse HTML (`<div class="dataset-file-metadata">`); o tema o oculta quando houver perfil v2.

## Compatibilidade

- **v1** (legado): objeto com `arquivo`, `linhas_totais`, `colunas_totais`, `esquema`. Não tem `schema_version`.
- **v2**: `schema_version: 2`. O tema só renderiza as abas para v2; para v1 ou JSON vazio/inválido, mantém o comportamento atual.

## Estrutura v2

```jsonc
{
  "schema_version": 2,
  "generated_at": "2026-11-04T12:00:00-03:00",   // ISO 8601
  "generator": "observadados-etl/profiler 2.0",
  "file": {
    "name": "mg-crime-violento-1-202605.csv",
    "format": "csv",              // csv | xlsx | xls
    "size_bytes": 35155959,
    "encoding": "utf-8",          // null para Excel
    "delimiter": ";",             // null para Excel
    "sheet": null                 // nome da aba lida, para Excel
  },
  "rows": 812345,                 // total de linhas de dados (sem cabeçalho)
  "rows_exact": true,             // false se for estimativa
  "columns": 14,
  "profiled_rows": 812345,        // linhas usadas nas estatísticas
  "sampled": false,               // true se as estatísticas vierem de amostra
  "sample": {
    "columns": ["data_fato", "municipio", "natureza", "qtde"],
    "rows": [["2024-01-03", "Belo Horizonte", "Roubo", "2"]]   // até 100 linhas; valores string ou null; strings truncadas em 200 chars
  },
  "fields": [ /* um objeto por coluna, na ordem do arquivo — ver abaixo */ ],
  "warnings": []                  // mensagens legíveis (ex.: "Coluna X tem 40% de valores ausentes")
}
```

### Objeto `fields[i]`

Comum a todos os tipos:

| chave | tipo | descrição |
|---|---|---|
| `name` | string | nome original da coluna |
| `position` | int | índice 0-based |
| `type` | string | `integer`, `numeric`, `categorical`, `text`, `date`, `datetime`, `boolean`, `empty` |
| `missing` | int | valores ausentes/vazios |
| `missing_pct` | number | 0–100, 2 casas |
| `unique` | int | valores distintos (não nulos) |
| `unique_is_estimate` | bool | |
| `description` | string \| null | reservado para o dicionário de dados (por ora `null`) |

Chaves adicionais por tipo (presentes só quando se aplicam):

- **`integer` / `numeric`**
  - `stats`: `{ "min", "max", "mean", "median", "std", "q1", "q3" }` (números JSON; nunca strings com vírgula)
  - `histogram`: `{ "bins": [b0, …, bn], "counts": [c1, …, cn] }` — 10 a 20 intervalos; `bins` tem `len(counts)+1` bordas
- **`categorical` / `boolean` / `text`**
  - `top`: `[{ "value": "Belo Horizonte", "count": 146000, "pct": 17.97 }, …]` — até 10, ordem decrescente
  - `other_pct`: number — % dos não nulos fora do `top`
- **`text`** (além de `top`)
  - `length`: `{ "min", "max", "mean" }`
- **`date` / `datetime`**
  - `range`: `{ "min": "2012-01-01", "max": "2025-11-30" }`
  - `timeline`: `{ "granularity": "year" | "month", "labels": ["2012", …], "counts": [n, …] }` — `month` se o intervalo for ≤ 36 meses, senão `year`

### Regras de inferência (ETL)

- Números em formato brasileiro (`1.234,56`) devem ser reconhecidos como numéricos.
- `categorical`: até 50 valores distintos, ou distintos ≤ 5% dos não nulos; caso contrário `text`.
- Códigos numéricos de identificação (ex.: código IBGE) podem ficar como `integer`; não é preciso tratá-los especialmente no MVP.
- Coluna totalmente vazia → `empty` (só as chaves comuns).
- Preferir estatísticas sobre o arquivo inteiro (leitura em chunks ou DuckDB). Se não for viável, `sampled: true` e `profiled_rows` com o tamanho da amostra.

## Exemplo mínimo válido

```json
{
  "schema_version": 2,
  "generated_at": "2026-11-04T12:00:00-03:00",
  "generator": "observadados-etl/profiler 2.0",
  "file": {"name": "exemplo.csv", "format": "csv", "size_bytes": 2048, "encoding": "utf-8", "delimiter": ";", "sheet": null},
  "rows": 3, "rows_exact": true, "columns": 3, "profiled_rows": 3, "sampled": false,
  "sample": {"columns": ["data_fato", "municipio", "qtde"], "rows": [["2024-01-03", "Belo Horizonte", "2"], ["2024-02-10", "Contagem", "1"], ["2025-03-01", null, "5"]]},
  "fields": [
    {"name": "data_fato", "position": 0, "type": "date", "missing": 0, "missing_pct": 0, "unique": 3, "unique_is_estimate": false, "description": null,
     "range": {"min": "2024-01-03", "max": "2025-03-01"}, "timeline": {"granularity": "month", "labels": ["2024-01", "2024-02", "2025-03"], "counts": [1, 1, 1]}},
    {"name": "municipio", "position": 1, "type": "categorical", "missing": 1, "missing_pct": 33.33, "unique": 2, "unique_is_estimate": false, "description": null,
     "top": [{"value": "Belo Horizonte", "count": 1, "pct": 50.0}, {"value": "Contagem", "count": 1, "pct": 50.0}], "other_pct": 0},
    {"name": "qtde", "position": 2, "type": "integer", "missing": 0, "missing_pct": 0, "unique": 3, "unique_is_estimate": false, "description": null,
     "stats": {"min": 1, "max": 5, "mean": 2.67, "median": 2, "std": 2.08, "q1": 1.5, "q3": 3.5},
     "histogram": {"bins": [1, 3, 5], "counts": [2, 1]}}
  ],
  "warnings": ["Coluna municipio tem 33,33% de valores ausentes"]
}
```
