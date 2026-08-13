import math
import os
import shutil
from datetime import datetime
from pathlib import Path

import httpx
import imageio_ffmpeg
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydub import AudioSegment
from pydub.exceptions import CouldntDecodeError

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

# Limite máximo aceito no upload — bem acima do limite do Whisper, só para
# evitar abuso; arquivos entre WHISPER_LIMIT_MB e este valor são divididos
# automaticamente antes de ir para a API.
MAX_UPLOAD_MB = 200
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Limite real do endpoint de transcrição do Whisper/Azure OpenAI.
WHISPER_LIMIT_MB = 25
WHISPER_LIMIT_BYTES = WHISPER_LIMIT_MB * 1024 * 1024

# Tamanho-alvo de cada pedaço ao dividir um áudio grande — abaixo do limite
# do Whisper com margem de segurança (a taxa de bits usada no re-encode dos
# pedaços é fixa, então o tamanho de saída fica previsível).
CHUNK_TARGET_MB = 20
CHUNK_TARGET_BYTES = CHUNK_TARGET_MB * 1024 * 1024
CHUNK_EXPORT_BITRATE = "128k"

# ffmpeg empacotado pelo imageio-ffmpeg: evita depender de um ffmpeg
# instalado no sistema (útil no Azure App Service Linux, onde o pacote
# padrão nem sempre está disponível sem passo extra de apt/packages).
AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()

database.init_db()

app = FastAPI(title="Transcrição de Áudio")


def verificar_token(x_access_token: str = Header(default=None)):
    if not ACCESS_TOKEN or x_access_token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="Token de acesso inválido")


def dividir_audio_por_tamanho(caminho_arquivo: Path, pasta_saida: Path) -> list[Path]:
    """Divide um áudio em pedaços de tamanho aproximado, cortando por tempo.

    O número de pedaços é calculado a partir do tamanho do arquivo original
    (ex: 60MB / 20MB ~= 3 pedaços). A duração total é então dividida por esse
    número de pedaços, e cada pedaço é decodificado e reexportado em uma taxa
    de bits fixa (CHUNK_EXPORT_BITRATE) — isso corta em limites de amostra
    real (não bytes arbitrários) e torna o tamanho de cada pedaço exportado
    previsível independentemente da taxa de bits do arquivo original.
    """
    tamanho_bytes = caminho_arquivo.stat().st_size
    num_partes = max(1, math.ceil(tamanho_bytes / CHUNK_TARGET_BYTES))

    # format/codec explícitos evitam que o pydub tente rodar "ffprobe" para
    # detectar o codec automaticamente — o imageio-ffmpeg só empacota o
    # binário do ffmpeg, então "ffprobe" não está disponível no ambiente.
    audio = AudioSegment.from_file(caminho_arquivo, format="mp3", codec="mp3")
    duracao_total_ms = len(audio)
    duracao_por_parte_ms = math.ceil(duracao_total_ms / num_partes)

    caminhos = []
    for indice in range(num_partes):
        inicio_ms = indice * duracao_por_parte_ms
        if inicio_ms >= duracao_total_ms:
            break
        fim_ms = min(inicio_ms + duracao_por_parte_ms, duracao_total_ms)
        parte = audio[inicio_ms:fim_ms]
        caminho_parte = pasta_saida / f"parte_{indice + 1:03d}.mp3"
        parte.export(caminho_parte, format="mp3", bitrate=CHUNK_EXPORT_BITRATE)
        caminhos.append(caminho_parte)

    return caminhos


def transcrever_chunk(caminho_chunk: Path) -> str:
    url = (
        f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/{AZURE_OPENAI_DEPLOYMENT}"
        f"/audio/transcriptions?api-version=2025-03-01-preview"
    )
    with open(caminho_chunk, "rb") as f:
        files = {"file": (caminho_chunk.name, f, "audio/mpeg")}
        data = {"language": "pt"}
        headers = {"api-key": AZURE_OPENAI_API_KEY}
        resposta = httpx.post(url, headers=headers, files=files, data=data, timeout=300)

    if resposta.status_code != 200:
        raise RuntimeError(f"Erro do Azure ({resposta.status_code}): {resposta.text}")

    return resposta.json().get("text", "")


def transcrever_arquivo(id: int, caminho_arquivo: Path) -> None:
    pasta_partes = caminho_arquivo.parent / f"{id}_partes"
    try:
        if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY or not AZURE_OPENAI_DEPLOYMENT:
            raise RuntimeError("Configuração do Azure OpenAI ausente no servidor")

        tamanho_bytes = caminho_arquivo.stat().st_size

        if tamanho_bytes <= WHISPER_LIMIT_BYTES:
            texto = transcrever_chunk(caminho_arquivo)
        else:
            pasta_partes.mkdir(exist_ok=True)
            partes = dividir_audio_por_tamanho(caminho_arquivo, pasta_partes)
            if not partes:
                raise RuntimeError("Não foi possível dividir o áudio para transcrição")

            total_partes = len(partes)
            textos = []
            for indice, parte in enumerate(partes, start=1):
                database.atualizar_progresso(id, f"Processando parte {indice} de {total_partes}")
                try:
                    textos.append(transcrever_chunk(parte))
                except Exception as exc:
                    raise RuntimeError(
                        f"Falha ao transcrever parte {indice} de {total_partes}: {exc}"
                    ) from exc

            texto = " ".join(t.strip() for t in textos if t.strip())

        database.concluir_transcricao(id, texto)
    except CouldntDecodeError as exc:
        database.marcar_erro(id, f"Falha ao processar o áudio (ffmpeg/pydub): {exc}")
    except Exception as exc:
        database.marcar_erro(id, str(exc))
    finally:
        caminho_arquivo.unlink(missing_ok=True)
        shutil.rmtree(pasta_partes, ignore_errors=True)


@app.post("/transcrever", dependencies=[Depends(verificar_token)])
async def transcrever(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Apenas arquivos .mp3 são aceitos")

    conteudo = await file.read()
    tamanho_bytes = len(conteudo)
    if tamanho_bytes > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"Arquivo excede o limite máximo de {MAX_UPLOAD_MB}MB")

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
