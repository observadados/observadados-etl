# 📊 Pipeline de ETL - ObservaDados

Este é o pipeline automatizado de **Extração, Transformação e Carga (ETL)** do projeto **ObservaDados**. Desenvolvido em Python, ele é responsável por varrer portais oficiais de segurança pública e defesa social de diversos estados brasileiros, baixar bases de dados brutas (CSV, XLSX, ZIP), extrair metadados estruturais (profiling) de forma automatizada e publicar tudo de maneira sincronizada e otimizada em um portal WordPress usando a API REST e Advanced Custom Fields (ACF).

---

## 🏗️ Arquitetura do Pipeline

O pipeline opera sob um modelo de **orquestração centralizada** e **execução em 4 fases**:

```mermaid
graph TD
    A[sources.csv] -->|1. Sincronização| B[(SQLite: collectors.db)]
    B -->|2. Varredura / Scrape| C{Páginas Estaduais}
    C -->|Banda Larga| D[Filtros CSS / XPath]
    D -->|3. Downloader Inteligente| E[source-files/ UF/]
    E -->|Se ZIP| F[Extração Temporária em Disco]
    E -->|4. Profiler de Dados| G[Perfil JSON v2]
    G -->|5. WordPress REST API| H[Biblioteca de Mídia & Posts do ACF]
```

1. **Sincronização (`sources.csv` ➔ SQLite)**: A planilha de configurações `sources.csv` atua como a fonte da verdade estrutural. O script sincroniza essas definições para um banco leve SQLite local (`collectors.db`), evitando chamadas repetidas e organizando o histórico.
2. **Varredura (Scrape & Discovery)**: O pipeline acessa as URLs configuradas. Se for uma página dinâmica, utiliza seletores CSS personalizados para identificar os links exatos de download de arquivos mais recentes do mês.
3. **Extração Inteligente (Download & Cache)**:
   - Baixa os arquivos diretamente para pastas organizadas por Unidade Federativa (UF), ex: `source-files/RS/`.
   - **Mecanismo de Cache**: Se o arquivo com a assinatura cronológica correspondente já existir localmente e a flag de sobrescrita não for informada, o download é ignorado.
   - **Bypass de Firewalls**: O robô emula User-Agents legítimos de navegadores para evitar bloqueios e cancelamentos de conexão (erro 10054) comuns em firewalls de órgãos públicos.
4. **Data Profiling Híbrido (Transform)**:
   - Lê o arquivo localmente. No caso de arquivos grandes compactados em `.zip` (ex: Rio Grande do Sul), o robô extrai temporariamente o arquivo interno para a pasta temporária do sistema operacional, faz a análise estrutural e exclui os arquivos temporários em seguida.
   - Lê o arquivo **inteiro** em blocos (pandas), detectando encoding (UTF-8 → cp1252/latin-1), delimitador e números no formato brasileiro (`1.234,56`).
   - Gera o perfil **JSON schema v2** (tipos, ausentes, distintos, estatísticas, histogramas, valores mais frequentes, linha do tempo de datas, amostra de 100 linhas e avisos), com no máximo 200 KB. O contrato está em [`docs/profile-schema.md`](docs/profile-schema.md); o tema WordPress só exibe esse JSON.
5. **Carga Otimizada (Load no WordPress)**:
   - **Modo Novo Arquivo**: Faz o upload do arquivo binário (ou ZIP original leve) para a biblioteca de mídia do WordPress. Associa-o ao Post do Dataset correspondente e grava o perfil JSON v2 no subcampo ACF `metadados` (não anexa mais HTML em `descricao`).
   - **Modo Delta/Recorrente (Sem Novo Arquivo)**: Se o arquivo já existe no servidor local e o dataset já está criado no WordPress, o robô faz uma chamada ultrarrápida (partial update) atualizando **apenas** o campo `data_ultima_varredura` no WP via API REST. Isso economiza megabytes de uploads duplicados e tempo de processamento.

---

## 📂 Estrutura de Diretórios do Projeto

```text
observadados-etl/
├── .env                  # Variáveis de ambiente locais (WP_URL, WP_USER, etc.) [Ignorado no Git]
├── .env.example          # Modelo de configuração de credenciais públicas
├── .gitignore            # Regras de exclusão do Git (ignora bases locais, logs e .db)
├── collectors.db         # Banco de dados SQLite local de histórico e controle [Ignorado no Git]
├── main.py               # Orquestrador principal do pipeline ETL
├── core/                 # Módulos centrais reutilizáveis do sistema
│   ├── base_scraper.py   # Web scraper base genérico com emulação de cabeçalhos Chrome
│   ├── db_manager.py     # Controle de banco SQLite e sincronização de fontes CSV
│   ├── downloader.py     # Downloader resiliente por blocos e descompactador ZIP
│   ├── mailer.py         # Módulo para envio de e-mails administrativos de relatório
│   ├── profiler.py       # Perfil JSON v2 (docs/profile-schema.md) com pandas e openpyxl
│   ├── reprofile.py      # --reprofile: regera `metadados` dos posts existentes
│   └── wp_client.py      # Integração completa com o WordPress (REST API & ACF)
├── scrapers/             # Scrapers específicos por portais de estados
│   ├── __init__.py       # Registro e fábrica de scrapers customizados
│   └── scraper_rj.py     # Scraper específico com regras avançadas para o Rio de Janeiro
├── static-sources/       # Configuração estática do robô
│   └── sources.csv       # Lista estruturada de fontes de dados e seletores
└── source-files/         # Repositório de arquivos brutos baixados [Ignorado no Git]
    ├── RJ/               # Arquivos locais organizados por UF
    └── RS/
```

---

## 🛠️ Configuração e Instalação

### 1. Pré-requisitos
* Python 3.10 ou superior instalado no sistema.

### 2. Clonar e Configurar Ambiente
Crie um ambiente virtual na pasta do projeto e instale as dependências requeridas:

```bash
# Criar ambiente virtual
python -m venv .venv

# Ativar ambiente virtual (Windows)
.venv\Scripts\activate

# Ativar ambiente virtual (Linux/macOS)
source .venv/bin/activate

# Instalar dependências necessárias
pip install pandas openpyxl requests beautifulsoup4
```

### 3. Configurar Variáveis de Ambiente (`.env`)
Copie o modelo de variáveis de ambiente para a raiz do projeto:

```bash
cp .env.example .env
```

Edite o arquivo `.env` recém-criado com as credenciais do seu WordPress local ou de produção:

```ini
WP_URL=https://observadados.org
WP_USER=admin_coletor
WP_APP_PASSWORD=xxxx xxxx xxxx xxxx xxxx
```
> [!IMPORTANT]
> A senha do WordPress (`WP_APP_PASSWORD`) deve ser uma **Senha de Aplicativo (Application Password)** gerada diretamente nas configurações do perfil do usuário no Painel do WordPress, e não a senha comum de login.

---

## 🚀 Como Executar o Pipeline

O orquestrador `main.py` aceita várias flags de comando para controle detalhado da execução:

### 1. Execução Padrão (Local)
Baixa as fontes ativas definidas no `sources.csv` que estão com a frequência de coleta expirada e armazena os arquivos localmente na pasta `source-files/`:

```bash
python main.py
```

### 2. Sincronizar e Publicar no WordPress
Baixa as novas fontes e publica arquivos e metadados estruturais na biblioteca de mídia e nos posts de tipo de conteúdo `dataset` no WordPress:

```bash
python main.py --publish-wordpress
```

**Como o post de cada coletor é encontrado** (`core/wp_resolver.py`): o vínculo coletor → post fica no próprio WordPress, nos metas `collector_key` e `collector_uf` do post `dataset` (registrados pelo tema em `inc/dataset-collector.php`, que precisa estar publicado antes). A busca segue esta ordem:

1. Post com o mesmo `collector_key`/`collector_uf`.
2. `wp_post_id` do `collector.db`, que serve só de cache.
3. Título idêntico, para posts antigos ainda sem meta. O post encontrado é adotado e recebe o meta.
4. Se nada for encontrado, cria um post novo.

Se a busca falhar (erro de rede ou de permissão, tema sem o meta, vários posts com o mesmo título), a publicação daquele arquivo é **cancelada** e nenhum post é criado. Por isso, `--reset-db` ou trocar de ambiente (`.env` local ou produção) não geram mais datasets duplicados. Recoletar o mesmo arquivo (por exemplo, com `--overwrite`) substitui a linha correspondente no repeater `arquivos`, em vez de acrescentar outra.

### 3. Forçar Sobrescrita e Download
Ignora a frequência de verificação local e o cache do disco, forçando o robô a realizar novamente o download de todos os arquivos de todas as fontes:

```bash
python main.py --overwrite --publish-wordpress
```

### 4. Limpar Arquivos Temporários e Locais
Exclui todos os diretórios e arquivos armazenados localmente em `source-files/` antes de rodar o novo ciclo de downloads:

```bash
python main.py --clean
```

### 5. Reinicializar Banco de Dados
Apaga completamente o banco SQLite local (`collectors.db`), limpando as tabelas e o histórico de downloads:

```bash
python main.py --reset-db
```

### 6. Reprocessar os perfis (`metadados`) já publicados
Percorre os posts `dataset` via REST, baixa cada arquivo do repeater `arquivos`, gera o perfil v2 e atualiza **apenas** o subcampo `metadados` de cada linha (os demais subcampos são preservados). Usa `.env.local` por padrão e recusa um `WP_URL` que não seja local, a menos que `--allow-remote` seja passado:

```bash
python main.py --reprofile --dry-run                     # não grava nada
python main.py --reprofile --dataset 432 --dataset 434   # só esses posts
python main.py --reprofile --dry-run --save-profiles perfis/
```

Para gerar o perfil de um arquivo local: `python -m core.profiler arquivo.csv -o perfil.json`. Testes: `python -m unittest discover -s tests -v`.

---

## ⚙️ Configurando Novas Fontes (`static-sources/sources.csv`)

O arquivo [sources.csv](file:///C:/var/www/observadados-etl/static-sources/sources.csv) controla quais sites o robô deve monitorar. Ele utiliza delimitador de ponto e vírgula (`;`) e a codificação `Windows-1252` para compatibilidade com acentos nativos em editores como Microsoft Excel.

### Principais Colunas do CSV:
* `collector_key`: Chave identificadora única do dataset (ex: `rs_seguranca`, `rj_feminicidio`).
* `uf`: Unidade federativa (ex: `RS`, `RJ`, `CE`).
* `url`: URL da página que contém os links de download ou a URL direta do arquivo.
* `is_active`: Define se a fonte será processada (`1` para ativo, `0` para ignorar).
* `acf_frequencia`: Frequência que dita o ciclo do robô (valores válidos: `diaria`, `semanal`, `mensal`, `anual`).
* `css_selector`: Seletor CSS usado pelo scraper dinâmico para encontrar os links de download na página (ex: `a[href$='.csv']` ou `a[href$='.zip']:not([href*='semestre'])`).
* `acf_origem`: Nome oficial do órgão que fornece os dados (ex: `Secretaria da Segurança Pública do Rio Grande do Sul`). Caso exista uma instituição cadastrada no WordPress sob esse nome (`post_type = instituicao`), o script criará automaticamente o relacionamento de posts!

---

## 🔒 Segurança e Versionamento (Git)

Como o pipeline lida com credenciais confidenciais de servidores do WordPress e consome grande volume de dados binários, o arquivo [`.gitignore`](file:///C:/var/www/observadados-etl/.gitignore) da pasta do projeto está configurado para garantir a segurança absoluta das suas chaves de acesso:

- **`.env` e `.env.*`**: **NUNCA** são enviados ao repositório no GitHub para proteger as senhas de produção do WordPress.
- **`collectors.db`**: Ignorado para evitar conflitos de mesclagem binários locais e histórico de execução específico de desenvolvedores.
- **`source-files/`**: Todos os gigabytes de dados brutos coletados ficam guardados apenas em cache local nas máquinas de execução para economizar armazenamento no repositório de código.
