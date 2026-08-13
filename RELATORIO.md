# Relatório do Projeto — transcricao-audio

Relatório do estado atual da implementação do app pessoal de transcrição de áudio.

---

## 1. Estrutura de arquivos criada

```
audio-transcript/
├── README.md                  Instruções de uso local e deploy
├── RELATORIO.md                Este relatório
└── app/
    ├── main.py                 App FastAPI: rotas, autenticação por token, integração com Azure Whisper
    ├── database.py              Setup e acesso ao SQLite (tabela transcricoes)
    ├── requirements.txt         Dependências Python do projeto
    ├── .env.example             Modelo das variáveis de ambiente necessárias
    ├── .gitignore                Ignora .env, transcricao.db, uploads/* e artefatos de Python
    ├── transcricao.db           (criado em runtime, não versionado) — banco SQLite
    ├── uploads/
    │   └── .gitkeep              Mantém a pasta versionada; os .mp3 recebidos são gravados aqui e apagados após o processamento
    └── static/
        ├── index.html            Página única: cabeçalho, botão de upload, tabela de histórico, modal de transcrição
        ├── style.css              Estilos: barra azul superior, botão de destaque, tabela, ícones de status, modal
        └── script.js              Lógica do frontend: token de acesso, upload com validação, polling, abertura do modal, copiar texto
```

---

## 2. Endpoints da API implementados

Todos exigem o header `x-access-token` (exceto os arquivos estáticos e a página `/`).

### `POST /transcrever`
- **Recebe:** `multipart/form-data` com campo `file` (arquivo `.mp3`)
- **Validações no backend:**
  - extensão do arquivo deve terminar em `.mp3` (senão `400`)
  - tamanho do arquivo (após leitura) não pode passar de 200MB (senão `400`) — ver [seção 11](#11-divisão-automática-de-áudios-grandes-por-tamanho-2026-08-13) para o porquê desse valor
- **Comportamento:**
  1. cria imediatamente um registro no banco com status `processando`
  2. salva o arquivo em `app/uploads/{id}_{nome_original}.mp3`
  3. dispara a transcrição em segundo plano (`BackgroundTasks` do FastAPI) — se o arquivo passar de 25MB (limite do Whisper), ele é dividido automaticamente em pedaços antes de ser enviado para a API (seção 11)
- **Retorna:** `{"id": <int>}` — id do registro criado, com status `202`-like (na prática `200`), imediatamente, sem esperar a transcrição terminar

### `GET /transcricoes`
- **Recebe:** nada (só o header de token)
- **Retorna:** lista de todos os registros, mais recente primeiro (`ORDER BY id DESC`), cada um como objeto JSON com todos os campos da tabela `transcricoes`

### `GET /transcricoes/{id}`
- **Recebe:** `id` na URL
- **Retorna:** objeto JSON com o registro completo (incluindo `texto_transcrito`), ou `404` se o id não existir

### `GET /` e `GET /static/*`
- Servem o frontend (`index.html`, `style.css`, `script.js`). Não exigem token — precisam ser acessíveis para o navegador carregar a página antes de o usuário informar o token.

---

## 3. Modelo de dados (SQLite)

Banco: `app/transcricao.db`, criado automaticamente em `database.init_db()` na inicialização do app.

### Tabela `transcricoes`

| Campo | Tipo | Observações |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | gerado automaticamente |
| `nome_arquivo` | `TEXT NOT NULL` | nome original do arquivo enviado |
| `data_envio` | `TEXT NOT NULL` | timestamp ISO 8601 (`datetime.now().isoformat()`), gerado no momento do upload |
| `tamanho_mb` | `REAL NOT NULL` | tamanho do arquivo em MB, arredondado a 2 casas decimais |
| `status` | `TEXT NOT NULL` | restrito por `CHECK` a `'processando'`, `'concluido'` ou `'erro'` |
| `texto_transcrito` | `TEXT` (nullable) | preenchido quando `status = 'concluido'` |
| `mensagem_erro` | `TEXT` (nullable) | preenchido quando `status = 'erro'`; para falha num pedaço de um áudio dividido, indica qual (ex: `"Falha ao transcrever parte 2 de 3: ..."`) |
| `progresso` | `TEXT` (nullable) | adicionada em 2026-08-13; preenchida durante o processamento de áudios divididos em pedaços (ex: `"Processando parte 2 de 3"`), limpa (`NULL`) ao concluir ou errar — ver [seção 11](#11-divisão-automática-de-áudios-grandes-por-tamanho-2026-08-13) |

A coluna `progresso` é adicionada via `ALTER TABLE ... ADD COLUMN` em `database.init_db()` (dentro de um `try/except sqlite3.OperationalError`), então bancos `transcricao.db` criados antes dessa mudança são migrados automaticamente na próxima inicialização do app, sem precisar apagar o arquivo.

Não há outras tabelas. O acesso é feito via `sqlite3` puro (sem ORM), com uma conexão nova por operação (`get_connection()`), usando `row_factory = sqlite3.Row` para permitir conversão direta para dicionário.

---

## 4. Fluxo completo do app, passo a passo

1. **Usuário abre `/`** → navegador carrega `index.html`, `style.css`, `script.js` (sem token, pois são estáticos).
2. **`script.js` roda `carregarLista()`** ao carregar a página → chama `GET /transcricoes`. Se não houver token salvo em `localStorage`, um `prompt()` pede o token antes da primeira chamada autenticada.
3. **Polling contínuo:** `setInterval` chama `carregarLista()` a cada 3 segundos, atualizando a tabela sem recarregar a página.
4. **Usuário clica em "+ Transcrever arquivo"** → abre o seletor de arquivos (`input[type=file]` oculto, aceita `.mp3`).
5. **Validação no frontend** (antes de qualquer chamada ao backend):
   - extensão deve ser `.mp3`
   - tamanho ≤ 25MB
   - se falhar, mostra mensagem de erro na tela e **não** chama o backend
6. **Envio:** `script.js` monta um `FormData` e faz `POST /transcrever` com o header de token.
7. **Backend recebe o upload:**
   - revalida extensão e tamanho (25MB) — nunca confia só na validação do frontend
   - cria o registro no SQLite com `status = 'processando'`
   - salva o `.mp3` em `app/uploads/`
   - agenda `transcrever_arquivo()` como `BackgroundTask` e responde imediatamente com o `id`
8. **Frontend recarrega a lista** logo após a resposta do upload → o novo item já aparece com o spinner de "Processando".
9. **Em segundo plano, o backend:**
   - monta a requisição `multipart/form-data` para o Azure OpenAI Whisper (`file` + `language=pt`), usando `AZURE_OPENAI_API_KEY` no header `api-key`
   - em caso de sucesso: extrai `text` da resposta JSON, chama `database.concluir_transcricao()` (status → `concluido`)
   - em caso de falha (erro HTTP do Azure, exceção de rede, config ausente): captura a exceção e chama `database.marcar_erro()` (status → `erro`, com a mensagem)
   - em ambos os casos, apaga o arquivo `.mp3` de `uploads/` no `finally`
10. **Próximo ciclo de polling** (até 3s depois) → o frontend detecta a mudança de status e re-renderiza a linha com o ícone verde (✔ concluído) ou vermelho (✖ erro).
11. **Usuário clica em um item concluído** → `abrirModal(id)` chama `GET /transcricoes/{id}`, abre o modal com o texto completo em `<pre>`.
12. **Botão "Copiar texto"** → `navigator.clipboard.writeText()` copia o conteúdo e mostra "Copiado!" por 2 segundos.

---

## 5. Variáveis de ambiente (`.env`)

Definidas em `app/.env` (não versionado), com modelo em `app/.env.example`:

| Variável | Para que serve |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | URL base do recurso Azure OpenAI (ex: `https://meu-recurso.openai.azure.com`) — usada para montar a URL da chamada de transcrição |
| `AZURE_OPENAI_API_KEY` | Chave de API do recurso Azure, enviada no header `api-key`. Nunca é exposta ao frontend, só lida no backend |
| `AZURE_OPENAI_DEPLOYMENT` | Nome do deployment do modelo Whisper configurado no Azure, usado no path da URL |
| `ACCESS_TOKEN` | Token simples exigido no header `x-access-token` em todas as chamadas de API (`/transcrever`, `/transcricoes`, `/transcricoes/{id}`) |

Se `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` ou `AZURE_OPENAI_DEPLOYMENT` não estiverem definidos, a transcrição falha em runtime e o registro é marcado como `erro` (validado durante os testes). Se `ACCESS_TOKEN` não estiver definido, todas as rotas de API retornam `401` sempre.

---

## 6. Dependências (`requirements.txt`)

| Pacote | Versão | Para que serve |
|---|---|---|
| `fastapi` | 0.115.0 | Framework web que expõe as rotas da API e serve os arquivos estáticos |
| `uvicorn[standard]` | 0.30.6 | Servidor ASGI que roda a aplicação FastAPI |
| `python-multipart` | 0.0.9 | Necessário para o FastAPI processar uploads `multipart/form-data` (`UploadFile`) |
| `httpx` | 0.27.2 | Cliente HTTP usado para chamar a API de transcrição do Azure OpenAI |
| `python-dotenv` | 1.0.1 | Carrega as variáveis do arquivo `.env` para o ambiente do processo |
| `imageio-ffmpeg` | 0.5.1 | Empacota um binário estático do `ffmpeg` multiplataforma — usado pelo `pydub` para decodificar/codificar áudio sem depender de um `ffmpeg` instalado no sistema |
| `pydub` | 0.25.1 | Manipulação de áudio (carregar, cortar por tempo, reexportar) usada para dividir arquivos grandes em pedaços antes de enviar ao Whisper — ver [seção 11](#11-divisão-automática-de-áudios-grandes-por-tamanho-2026-08-13) |
| `audioop-lts` | 0.2.1 (só em Python ≥ 3.13) | O `pydub` depende do módulo `audioop`, removido da biblioteca padrão a partir do Python 3.13; este pacote o reimplementa. Marcado como condicional (`; python_version >= "3.13"`) no `requirements.txt` — não instala em Python 3.11 (o runtime usado no Azure App Service), onde `audioop` ainda é nativo; só é necessário para rodar localmente em máquinas com Python 3.13+ |

`sqlite3` não está listado por ser parte da biblioteca padrão do Python.

---

## 7. O que falta para rodar localmente

1. Ter Python 3.11 instalado (versão usada nos testes).
2. Criar e ativar um ambiente virtual dentro de `app/`:
   ```bash
   cd app
   python -m venv .venv
   .venv\Scripts\activate
   ```
3. Instalar as dependências:
   ```bash
   pip install -r requirements.txt
   ```
4. Copiar `.env.example` para `.env` e preencher com valores reais:
   ```bash
   cp .env.example .env
   ```
   - `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT` — precisam de um recurso Azure OpenAI real com um deployment Whisper já criado (isso ainda não existe/não foi configurado neste projeto).
   - `ACCESS_TOKEN` — qualquer string escolhida pelo usuário.
5. Rodar o servidor:
   ```bash
   uvicorn main:app --reload
   ```
6. Acessar `http://localhost:8000` e informar o token quando solicitado pelo navegador.

Sem um recurso Azure OpenAI real configurado, o app sobe e a listagem/upload funcionam, mas toda transcrição vai cair em `status = 'erro'`.

---

## 8. O que falta para o deploy no Azure App Service

1. Ter uma assinatura Azure e o Azure CLI (`az`) instalado e autenticado (`az login`).
2. Ter (ou criar) um recurso Azure OpenAI com deployment do modelo Whisper — pré-requisito separado do deploy do App Service.
3. Rodar o deploy a partir da pasta `app/`:
   ```bash
   az webapp up --name <nome> --resource-group <rg> --sku F1 --runtime "PYTHON:3.11"
   ```
4. Configurar o **Startup Command** no App Service (não é feito automaticamente pelo `az webapp up`):
   ```
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```
5. Definir as variáveis de ambiente em **Configuration > Application settings** (ou via `az webapp config appsettings set`) — o `.env` local não é enviado no deploy:
   - `AZURE_OPENAI_ENDPOINT`
   - `AZURE_OPENAI_API_KEY`
   - `AZURE_OPENAI_DEPLOYMENT`
   - `ACCESS_TOKEN`
6. Confirmar que a porta e o host da app batem com o esperado pelo App Service (`0.0.0.0:8000` ou a porta fornecida pela variável `PORT`, se o Azure exigir).
7. Nenhum desses passos foi executado ainda — o deploy real no Azure App Service não foi testado nesta sessão, apenas documentado no README.

---

## 9. Limitações conhecidas e simplificações feitas

- **SQLite local em arquivo único** — sem servidor de banco separado; não é adequado para múltiplos usuários simultâneos ou alta concorrência de escrita, mas é suficiente para uso pessoal.
- **Processamento em `BackgroundTask` do FastAPI, não fila/worker dedicado** — mais simples de operar (sem infraestrutura extra), mas o processamento ainda roda no mesmo processo do servidor web; uma transcrição longa consome recursos do processo que também atende requisições HTTP.
- **Atualização por polling (3s), não WebSocket** — mais simples de implementar e depurar, mas gera requisições HTTP repetidas mesmo sem mudança de estado, e a UI pode levar até ~3s para refletir uma conclusão.
- **Limite de upload de 200MB, com divisão automática acima de 25MB** — o limite de 25MB é do serviço Whisper no Azure OpenAI, não do app; arquivos entre 25MB e 200MB são divididos automaticamente em pedaços antes da transcrição (seção 11). 200MB é um teto arbitrário só para evitar upload/processamento de arquivos absurdamente grandes, não um limite técnico do Whisper.
- **Divisão de áudio decodifica o arquivo inteiro em memória (`pydub`)** — para um áudio de ~1h em mp3, a versão decodificada (PCM) em RAM fica na casa de várias centenas de MB. Isso não foi testado sob as restrições reais de memória do plano F1 do Azure App Service (documentadas como ~1GB); se o processo estourar memória em produção, a mitigação seria trocar a decodificação completa via `pydub` por um corte via `ffmpeg` usando apenas os timestamps (sem carregar o PCM inteiro na RAM).
- **Token único e simples (`ACCESS_TOKEN`)** — não há múltiplos usuários, papéis, expiração ou rotação de token; adequado para uso pessoal, não para múltiplos usuários com necessidades de acesso diferentes.
- **Arquivos `.mp3` descartados após a transcrição** — o áudio original não fica disponível para reprocessamento; se a transcrição falhar, é preciso reenviar o arquivo.
- **Sem paginação na listagem** — `GET /transcricoes` sempre retorna o histórico inteiro; para um histórico muito grande isso pode ficar lento.
- **Sem retry automático** — se a chamada ao Azure falhar (rede instável, rate limit), o registro fica marcado como `erro` permanentemente; o usuário precisa reenviar o arquivo manualmente.
- **Idioma fixo em `pt`** — o parâmetro `language=pt` está hardcoded na chamada ao Azure, não há suporte a outros idiomas pela UI.
- **Armazenamento no plano F1 do Azure** — conforme já documentado no README, o plano gratuito tem armazenamento limitado; nem o banco SQLite nem os uploads têm rotina automática de limpeza além do descarte do `.mp3` logo após o processamento.

---

## 10. Pontos ainda não testados

- ~~Integração real com o Azure OpenAI Whisper~~ — **validada em 2026-08-07.** Foi gerado um `.mp3` real (fala sintetizada via SAPI do Windows, codificada em mp3 com `lameenc`) e enviado via `POST /transcrever` contra o recurso Azure real (`wdo-mkhacjbb-eastus2`). O texto retornado bateu exatamente com o áudio falado. Nesse teste foi descoberto que o nome do deployment não era `gpt-transcribe` (retornava `404 DeploymentNotFound`), e sim `gpt-4o-transcribe` — corrigido em `app/.env`.
- **Deploy no Azure App Service** — não foi executado; o comando `az webapp up` e a configuração de app settings/startup command não foram validados na prática.
- **Comportamento no plano F1 sob carga real** — limites de CPU/memória do F1, cold start, e persistência de arquivo em reinícios não foram observados.
- **Uso do app pelo navegador (UI manual)** — os testes realizados foram via `curl` (backend); a interface (`index.html`/`script.js`) ainda não foi verificada visualmente num navegador em uso normal (fluxo do `prompt()` de token, o modal, o botão de copiar, ícones de status). Um bug de CSS já identificado por captura de tela do usuário (modal aparecendo aberto por padrão) foi corrigido em `style.css` (`.modal[hidden] { display: none; }`), mas o restante da UI segue sem verificação visual.
- ~~Arquivo maior que 25MB~~ — **validado em 2026-08-13**, ver [seção 11](#11-divisão-automática-de-áudios-grandes-por-tamanho-2026-08-13).
- **Extensão inválida (não `.mp3`)** — a rejeição de arquivos com outra extensão não foi testada via requisição real.
- **Renovação/expiração do token inválido em uso** — o comportamento do frontend ao receber `401` no meio do uso (token trocado ou expirado) não foi testado interativamente.
- **Concorrência** — múltiplos uploads simultâneos ou uploads durante o polling não foram testados.

---

## 11. Divisão automática de áudios grandes por tamanho (2026-08-13)

### Motivação

Em teste real com uma reunião de ~1h, dois problemas apareceram:

1. **Arquivos acima de 25MB eram rejeitados no upload** (`MAX_SIZE_BYTES`), mesmo sendo esse um limite do serviço Whisper, não do app — não havia como transcrever um áudio de reunião longo.
2. **Uma transcrição enviada por inteiro voltou incompleta** (faltou parte do texto) — mesma causa raiz: o arquivo estava perto/acima do limite de tamanho processado de uma vez pela API.

### O que mudou

- **Upload:** o limite passou de 25MB (rejeitava o arquivo) para **200MB** (`MAX_UPLOAD_MB` em `app/main.py`) — um teto arbitrário só contra abuso, bem acima do necessário.
- **Transcrição — dois caminhos**, decididos pelo tamanho do arquivo já salvo em disco (`WHISPER_LIMIT_BYTES` = 25MB):
  - **≤ 25MB:** comportamento inalterado — o arquivo original é enviado direto para o Whisper numa única chamada.
  - **> 25MB:** o arquivo é dividido em pedaços antes da transcrição (`dividir_audio_por_tamanho()` em `app/main.py`):
    1. o **número de pedaços** é calculado a partir do **tamanho do arquivo**: `num_partes = ceil(tamanho_bytes / CHUNK_TARGET_BYTES)`, com `CHUNK_TARGET_MB = 20` (ex: 60MB → 3 pedaços de ~20MB).
    2. a **duração total** do áudio (via `pydub`/`ffmpeg`) é dividida em `num_partes` intervalos de tempo iguais — o corte acontece em amostras decodificadas (não em bytes arbitrários do arquivo comprimido), então nunca corte no meio de um frame de áudio.
    3. cada pedaço é reexportado como `.mp3` numa **taxa de bits fixa** (`CHUNK_EXPORT_BITRATE = "128k"`), o que torna o tamanho de saída de cada pedaço previsível e independente da taxa de bits do arquivo original (o cálculo do passo 1 assume implicitamente uma taxa de bits ~constante; reexportar numa taxa fixa é o que garante essa suposição na prática).
    4. cada pedaço é enviado ao Whisper **em sequência** (não em paralelo); o texto de cada resposta é acumulado numa lista e concatenado com espaço (`" ".join(...)`) ao final, na ordem original — evita palavras coladas no ponto de corte.
    5. o registro no banco só é atualizado (`status = 'concluido'`, `texto_transcrito`) **depois que todos os pedaços forem transcritos com sucesso**. Se um pedaço falhar, a exceção é reformulada como `"Falha ao transcrever parte {N} de {total}: {erro original}"` e o registro inteiro vai para `status = 'erro'` com essa mensagem — nada é salvo parcialmente.
    6. em qualquer caso (sucesso ou erro), o arquivo original e a pasta de pedaços temporários (`uploads/{id}_partes/`) são apagados no `finally` — sem lixo acumulando no disco do plano F1.
- **Progresso visível:** nova coluna `progresso` na tabela `transcricoes` (migração automática via `ALTER TABLE`), atualizada a cada pedaço (`"Processando parte 2 de 3"`) e limpa ao concluir/errar. O frontend (`script.js`) mostra esse texto como uma segunda linha, menor e cinza, abaixo do spinner "Processando" — só aparece quando o áudio está sendo dividido; para arquivos que não precisam de divisão, o status continua como antes.
- **Frontend:** `MAX_SIZE_MB` em `script.js` atualizado de 25 para 200 (a mensagem de validação já é gerada a partir dessa constante, então passou a dizer "200MB" automaticamente, sem string hardcoded para trocar).

### Armadilha encontrada durante o teste: `pydub` precisa de `ffprobe`, não só de `ffmpeg`

O `pydub.AudioSegment.from_file()` sempre chama `ffprobe` internamente para inspecionar o arquivo antes de decodificar — mas o pacote `imageio-ffmpeg` (usado para não depender de um `ffmpeg` do sistema) só empacota o binário do `ffmpeg`, não o `ffprobe`. Isso quebrava com `[WinError 2] O sistema não pode encontrar o arquivo especificado` assim que um áudio grande chegava para ser dividido.

**Correção:** passar `format="mp3", codec="mp3"` para `AudioSegment.from_file()`. Quando um `codec` é informado explicitamente, o `pydub` pula a chamada ao `ffprobe` (ele só a usa para *auto-detectar* o codec de entrada) — como o app já garante que todo upload é `.mp3` (validado na extensão), forçar o codec é seguro e elimina a dependência de `ffprobe`.

### ffmpeg no ambiente de deploy (Azure App Service Linux)

Não é necessário instalar `ffmpeg` via `apt`/`packages` no App Service: o app usa o binário estático empacotado pelo `imageio-ffmpeg` (`imageio_ffmpeg.get_ffmpeg_exe()`), independente do que estiver (ou não) instalado no sistema operacional do container. Isso já era verdade antes desta mudança (decisão tomada no commit anterior, que introduziu `imageio-ffmpeg`) e continua valendo aqui. Caso um dia se opte por usar um `ffmpeg` do sistema em vez do empacotado, seria necessário adicionar um arquivo `aptPackages.txt`/`packages.txt` com `ffmpeg` na raiz do deploy (mecanismo do Oryx/App Service Linux para instalar pacotes apt) — mas isso não foi feito nem é necessário com a abordagem atual.

### Teste de ponta a ponta realizado

Como não havia à mão uma gravação real de reunião acima de 25MB, foi gerado um áudio sintético para o teste:
- dois trechos curtos de fala (SAPI do Windows) — um "de abertura" e um "de encerramento", com frases distintas para dá pra checar visualmente se a ordem das partes bate;
- preenchido no meio com um tom senoidal gerado via `ffmpeg` (`sine=frequency=440`) até o arquivo final somar **29,26MB** a 128kbps (~32 min de áudio).

Resultado, contra o recurso Azure real (`wdo-mkhacjbb-eastus2`, deployment `gpt-4o-transcribe`):
- o arquivo foi dividido automaticamente em **2 pedaços** (29,26MB / 20MB → `ceil = 2`), como esperado;
- a coluna `progresso` mostrou corretamente `"Processando parte 1 de 2"` e depois `"Processando parte 2 de 2"` durante o processamento (poll via `GET /transcricoes/{id}`);
- status final `concluido`, com o texto dos dois pedaços concatenado **na ordem correta** — a frase de encerramento (só presente no 2º pedaço) apareceu depois da frase de abertura (só presente no 1º), sem sobreposição nem repetição;
- o arquivo original e a pasta `uploads/{id}_partes/` foram apagados ao final — confirmado via listagem do diretório após a conclusão;
- também foi testado (mesmo teste, arquivo pequeno de 0,12MB) que o **caminho sem divisão continua funcionando** sem diferenças;
- também foi simulada uma **falha no 2º de 2 pedaços** (chamada à API monkeypatchada para lançar erro): o registro foi corretamente marcado como `status = 'erro'` com `mensagem_erro = "Falha ao transcrever parte 2 de 2: Erro simulado do Azure (429)"`, e os arquivos temporários também foram limpos nesse caso.

**Observação sobre qualidade da transcrição no teste:** o texto do primeiro pedaço (fala + ~14min de tom senoidal) saiu parcialmente incompreensível/alucinado pelo modelo — comportamento conhecido de modelos da família Whisper quando processam longos trechos de áudio sem fala real (não é um problema da lógica de divisão/concatenação, que preservou corretamente a ordem e a integridade dos dois pedaços). Numa gravação real de reunião, com fala contínua, esse efeito não é esperado.

### Limitação conhecida não testada

A decodificação do áudio inteiro em memória pelo `pydub` antes da divisão (ver seção 9) não foi testada sob as restrições reais de memória do plano F1 do Azure App Service — o teste acima rodou localmente, sem esse limite.
