# Contrato do cubo de painel (`observadados/painel-cubo`, schema 1)

Compartilhado entre **observadados-etl** (`aggregate/`, produz) e **tema-observadados** (`inc/painel.php` + `js/painel.js`, consome).
Existe uma cópia idêntica em cada repositório; qualquer mudança deve ser feita nas duas.

## Transporte

- O ETL envia o JSON no corpo de `POST /wp-json/observadados/v1/painel/{slug}/cubo` (autenticação por senha de aplicativo; exige `edit_posts`).
- O tema valida `schema`, `painel === slug` e a presença de `dims`, `municipios` e `municipal_anual`, grava em `uploads/paineis/{slug}.json` e guarda `{file, bytes, sha1, generated_at, generator, received_at}` no meta `_painel_cubo` do post `painel` de mesmo slug. Se o post não existir, cria um **rascunho**.
- Limite: 8 MB. O front-end baixa o arquivo estático (`?v=` com o sha1 para invalidar cache).

## Estrutura

```jsonc
{
  "schema": "observadados/painel-cubo",
  "schema_version": 1,
  "painel": "violencia-letal",
  "titulo": "Violência letal",
  "generated_at": "2026-11-05T00:00:00-03:00",
  "generator": "observadados-etl/aggregate.violencia_letal 1.0",
  "dims": {
    "uf": ["AL", "BA", ...],
    "natureza": [{ "id": "homicidio_doloso", "label": "Homicídio doloso" }, { "id": "outros_cvli", "label": "...", "residual": true }],  // residual: ausência não gera aviso de cobertura parcial
    "sexo": [{ "id": "M", "label": "Masculino" }, { "id": "F", ... }, { "id": "NI", ... }],
    "faixa": ["0–14", ..., "60+", "Não informada"],   // a última é sempre "não informada"
    "anos": [2001, ..., 2026]                         // contínuo
  },
  "indicadores": [{ "id": "homicidio", "label": "...", "naturezas": [0, 1] }],  // índices em dims.natureza; o primeiro é o padrão
  "cobertura": {
    "AL": {
      "inicio": "2012-01", "fim": "2026-08",          // meses publicados (usados para anualizar taxas)
      "naturezas": ["homicidio_doloso", ...],          // ids publicados pela UF
      "sexo_idade": true,                              // false: UF fica fora de recortes por sexo/idade
      "vitimas": 21526, "sem_municipio": 0, "sem_data": 0, "antes_de_2001": 0,
      "orgao": "...", "collector_key": "al_cvli", "arquivos": ["al-cvli-202610.csv"], "notas": ["..."]
    }
  },
  "municipios": { "cod": [2700102, ...], "nome": ["Água Branca", ...], "uf": [0, ...] },  // uf = índice em dims.uf
  "populacao": {
    "fonte": "...",
    "origem_por_ano": { "2007": "interpolado (2006–2008)", ... },
    "valores": [[...], ...]          // valores[i_ano][i_municipio], alinhado a dims.anos e municipios
  },
  "municipal_anual": {                // município × ano × natureza × sexo × faixa
    "cols": ["mun", "ano", "nat", "sx", "faixa", "n"],
    "rows": 70910,
    "ano_offset": true,               // ano = valor + dims.anos[0]
    "delta": ["mun"],                 // valor real = soma acumulada (linhas ordenadas por essa coluna)
    "data": { "mun": [...], "ano": [...], ... }
  },
  "uf_mensal": {                      // UF × ano × mês × natureza × sexo × faixa (inclui vítimas sem município)
    "cols": ["uf", "ano", "mes", "nat", "sx", "faixa", "n"], "ano_offset": true, "delta": ["uf"], ...
  },
  "nao_casados": [{ "uf": "PE", "valor": "NAO INFORMADO", "vitimas": 9 }]
}
```

## Regras de uso no front-end

- Totais de UF e séries sem município selecionado vêm de `uf_mensal`; mapa, ranking e recortes municipais vêm de `municipal_anual`.
- Taxa = vítimas ÷ Σ(população × meses publicados ÷ 12) × 100 mil, somando só anos/meses cobertos pela UF.
- Com filtro de sexo ou faixa etária, não há taxa (sem população por sexo/idade) e UFs com `sexo_idade: false` saem do recorte.
