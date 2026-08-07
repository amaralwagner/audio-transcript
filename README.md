# transcricao-audio

App pessoal de transcrição de áudio (.mp3) usando FastAPI + Azure OpenAI Whisper, com histórico em SQLite.

## Rodando localmente

```bash
cd app
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
cp .env.example .env        # preencha as variáveis do Azure e o ACCESS_TOKEN
uvicorn main:app --reload
```

Acesse `http://localhost:8000`. Na primeira ação (upload ou carregamento da lista), o navegador vai pedir o token de acesso — use o mesmo valor definido em `ACCESS_TOKEN` no `.env`.

## Variáveis de ambiente (`.env`)

| Variável | Descrição |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | URL base do recurso Azure OpenAI |
| `AZURE_OPENAI_API_KEY` | Chave de API do recurso |
| `AZURE_OPENAI_DEPLOYMENT` | Nome do deployment do modelo Whisper |
| `ACCESS_TOKEN` | Token simples exigido no header `x-access-token` em todas as chamadas de API |

## Deploy no Azure App Service (plano F1 - gratuito)

```bash
az webapp up --name <nome> --resource-group <rg> --sku F1 --runtime "PYTHON:3.11"
```

Configure o startup command:

```
uvicorn main:app --host 0.0.0.0 --port 8000
```

Defina as variáveis de ambiente (`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `ACCESS_TOKEN`) em **Configuration > Application settings** no portal do Azure (ou via `az webapp config appsettings set`).

> **Atenção:** no plano F1 o armazenamento é limitado e não persiste garantidamente entre reinícios. O banco `transcricao.db` e os arquivos em `/uploads` podem precisar de limpeza manual periódica. Os arquivos `.mp3` enviados já são apagados automaticamente após a transcrição (com sucesso ou erro), mas o `transcricao.db` cresce com o histórico de textos transcritos.

## Estrutura

```
app/
  static/          index.html, style.css, script.js
  uploads/         arquivos mp3 recebidos (descartados após transcrever)
  main.py          rotas FastAPI e integração com Azure Whisper
  database.py      setup e acesso ao SQLite
  transcricao.db   criado em runtime
  requirements.txt
  .env.example
```
