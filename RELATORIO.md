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
  - tamanho do arquivo (após leitura) não pode passar de 25MB (senão `400`)
- **Comportamento:**
  1. cria imediatamente um registro no banco com status `processando`
  2. salva o arquivo em `app/uploads/{id}_{nome_original}.mp3`
  3. dispara a transcrição em segundo plano (`BackgroundTasks` do FastAPI)
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
| `mensagem_erro` | `TEXT` (nullable) | preenchido quando `status = 'erro'` |

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
- **Limite fixo de 25MB** — corresponde ao limite do serviço Whisper no Azure OpenAI; arquivos maiores precisariam de compressão/chunking, que não foi implementado.
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
- **Arquivo maior que 25MB** — a validação de tamanho (frontend e backend) não foi exercitada com um arquivo real acima do limite.
- **Extensão inválida (não `.mp3`)** — a rejeição de arquivos com outra extensão não foi testada via requisição real.
- **Renovação/expiração do token inválido em uso** — o comportamento do frontend ao receber `401` no meio do uso (token trocado ou expirado) não foi testado interativamente.
- **Concorrência** — múltiplos uploads simultâneos ou uploads durante o polling não foram testados.
