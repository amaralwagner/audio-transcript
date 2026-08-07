import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import database

load_dotenv()

APP_DIR = Path(__file__).parent
UPLOADS_DIR = APP_DIR / "uploads"
STATIC_DIR = APP_DIR / "static"
UPLOADS_DIR.mkdir(exist_ok=True)

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")

MAX_SIZE_MB = 25
MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024

database.init_db()

app = FastAPI(title="Transcrição de Áudio")


def verificar_token(x_access_token: str = Header(default=None)):
    if not ACCESS_TOKEN or x_access_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="Token de acesso inválido")


def transcrever_arquivo(id: int, caminho_arquivo: Path) -> None:
    try:
        if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY or not AZURE_OPENAI_DEPLOYMENT:
            raise RuntimeError("Configuração do Azure OpenAI ausente no servidor")

        url = (
            f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/{AZURE_OPENAI_DEPLOYMENT}"
            f"/audio/transcriptions?api-version=2025-03-01-preview"
        )
        with open(caminho_arquivo, "rb") as f:
            files = {"file": (caminho_arquivo.name, f, "audio/mpeg")}
            data = {"language": "pt"}
            headers = {"api-key": AZURE_OPENAI_API_KEY}
            resposta = httpx.post(url, headers=headers, files=files, data=data, timeout=300)

        if resposta.status_code != 200:
            raise RuntimeError(f"Erro do Azure ({resposta.status_code}): {resposta.text}")

        texto = resposta.json().get("text", "")
        database.concluir_transcricao(id, texto)
    except Exception as exc:
        database.marcar_erro(id, str(exc))
    finally:
        caminho_arquivo.unlink(missing_ok=True)


@app.post("/transcrever", dependencies=[Depends(verificar_token)])
async def transcrever(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Apenas arquivos .mp3 são aceitos")

    conteudo = await file.read()
    tamanho_bytes = len(conteudo)
    if tamanho_bytes > MAX_SIZE_BYTES:
        raise HTTPException(status_code=400, detail=f"Arquivo excede o limite de {MAX_SIZE_MB}MB")

    tamanho_mb = round(tamanho_bytes / (1024 * 1024), 2)
    data_envio = datetime.now().isoformat(timespec="seconds")
    id = database.criar_transcricao(file.filename, data_envio, tamanho_mb)

    caminho_arquivo = UPLOADS_DIR / f"{id}_{file.filename}"
    with open(caminho_arquivo, "wb") as f:
        f.write(conteudo)

    background_tasks.add_task(transcrever_arquivo, id, caminho_arquivo)

    return {"id": id}


@app.get("/transcricoes", dependencies=[Depends(verificar_token)])
def listar():
    return [dict(row) for row in database.listar_transcricoes()]


@app.get("/transcricoes/{id}", dependencies=[Depends(verificar_token)])
def detalhe(id: int):
    row = database.obter_transcricao(id)
    if row is None:
        raise HTTPException(status_code=404, detail="Transcrição não encontrada")
    return dict(row)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
