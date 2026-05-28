import sys
import json
from pathlib import Path


def profile_file(file_path: Path) -> tuple[str, str]:
    """
    Analisa um arquivo CSV ou Excel e extrai metadados estruturais (linhas, colunas, tipos).
    Retorna uma tupla: (json_string, html_string).
    """
    try:
        import pandas as pd
    except ImportError:
        print("Pandas não está instalado.", file=sys.stderr)
        return "{}", "<p>Pandas não instalado.</p>"

    try:
        ext = file_path.suffix.lower()
        if ext == ".csv":
            try:
                # Tenta ler com utf-8 primeiro
                df = pd.read_csv(
                    file_path, nrows=5000, sep=None, engine="python", encoding="utf-8"
                )
            except Exception:
                # Fallback para cp1252 (comum no Windows/Excel em pt-br)
                df = pd.read_csv(
                    file_path, nrows=5000, sep=None, engine="python", encoding="cp1252"
                )

            # Conta o total de linhas do CSV de forma eficiente (sem carregar no Pandas)
            try:
                with open(file_path, "rb") as f:
                    total_rows = sum(1 for _ in f) - 1  # -1 para o cabeçalho
            except Exception:
                total_rows = "N/A"

        elif ext in [".xlsx", ".xls"]:
            # Para excel, o pandas geralmente lê tudo na memória
            df = pd.read_excel(file_path, nrows=5000)
            total_rows = "10.000+ (Estimado)"  # Excel completo pode ser lento, usamos estimativa ou carregamos tudo se o arquivo for pequeno
            # Tentar pegar linhas via engine se precisar, mas o ideal é o nrows pro excel não estourar a RAM
        else:
            return "{}", f"<p>Formato não suportado para metadados: {ext}</p>"

        columns_info = []
        for col in df.columns:
            dtype_str = str(df[col].dtype)
            friendly_type = "Texto"
            if "int" in dtype_str:
                friendly_type = "Inteiro"
            elif "float" in dtype_str:
                friendly_type = "Decimal"
            elif "datetime" in dtype_str:
                friendly_type = "Data/Hora"
            elif "bool" in dtype_str:
                friendly_type = "Booleano"

            null_count = int(df[col].isnull().sum())

            columns_info.append(
                {
                    "nome": str(col),
                    "tipo_inferido": friendly_type,
                    "nulos_amostra": null_count,
                }
            )

        metadata = {
            "arquivo": file_path.name,
            "linhas_totais": total_rows,
            "colunas_totais": len(df.columns),
            "esquema": columns_info,
        }

        json_string = json.dumps(metadata, ensure_ascii=False, indent=2)

        # Build HTML table
        html = f"""
        <div class="dataset-file-metadata">
            <h4>Visão Geral dos Dados</h4>
            <ul>
                <li><strong>Total de Linhas:</strong> {total_rows}</li>
                <li><strong>Total de Colunas:</strong> {len(df.columns)}</li>
            </ul>
            <table class="metadata-table">
                <thead>
                    <tr>
                        <th>Nome da Coluna</th>
                        <th>Tipo (Amostra)</th>
                        <th>Valores Nulos (Amostra)</th>
                    </tr>
                </thead>
                <tbody>
        """
        for col_info in columns_info:
            html += f"""
                    <tr>
                        <td><code>{col_info["nome"]}</code></td>
                        <td>{col_info["tipo_inferido"]}</td>
                        <td>{col_info["nulos_amostra"]}</td>
                    </tr>
            """
        html += """
                </tbody>
            </table>
            <p>* A inferência de tipos e nulos foi baseada em uma amostra inicial do arquivo.</p>
        </div>
        """

        return json_string, html

    except Exception as e:
        print(f"⚠️ Erro ao extrair metadados de {file_path.name}: {e}", file=sys.stderr)
        return "{}", f"<p>Erro ao extrair metadados do arquivo.</p>"
