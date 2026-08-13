import math
import os
import re
import shutil
import subprocess
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

# Limite real do endpoint de transcrição do Whisper/Azure OpenAI. Dois
# limites independentes: tamanho do arquivo E duração — um áudio pode ser
# pequeno em bytes (taxa de bits baixa) e ainda assim durar mais que
# WHISPER_MAX_DURATION_SECONDS (visto na prática: 22,58MB / 52,8min).
WHISPER_LIMIT_MB = 25
WHISPER_LIMIT_BYTES = WHISPER_LIMIT_MB * 1024 * 1024
WHISPER_MAX_DURATION_SECONDS = 1500

# Alvos de cada pedaço ao dividir um áudio grande — abaixo dos limites do
# Whisper com margem de segurança (a taxa de bits usada no re-encode dos
# pedaços é fixa, então o tamanho de saída fica previsível). O número de
# pedaços usado é o maior entre o exigido pelo tamanho e pela duração.
CHUNK_TARGET_MB = 20
CHUNK_TARGET_BYTES = CHUNK_TARGET_MB * 1024 * 1024
CHUNK_TARGET_DURATION_SECONDS = 1200
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


def obter_duracao_segundos(caminho_arquivo: Path) -> float:
    """Lê a duração do áudio a partir do cabeçalho reportado pelo ffmpeg.

    Roda "ffmpeg -i arquivo" sem definir uma saída: o ffmpeg lê os metadados
    do arquivo, imprime a duração no stderr e sai com erro (nenhuma saída foi
    pedida) — mas não precisamos do código de saída, só do texto. É bem mais
    barato que decodificar o áudio inteiro (que só é feito depois, e apenas
    se a divisão for realmente necessária).
    """
    resultado = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(caminho_arquivo)],
        capture_output=True,
        text=True,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", resultado.stderr)
    if not match:
        raise RuntimeError("Não foi possível determinar a duração do áudio")
    horas, minutos, segundos = match.groups()
    return int(horas) * 3600 + int(minutos) * 60 + float(segundos)


def dividir_audio_por_tamanho(caminho_arquivo: Path, pasta_saida: Path, num_partes: int) -> list[Path]:
    """Divide um áudio em `num_partes` pedaços de duração igual.

    Cada pedaço é decodificado e reexportado em uma taxa de bits fixa
    (CHUNK_EXPORT_BITRATE) — isso corta em limites de amostra real (não
    bytes arbitrários) e torna o tamanho de cada pedaço exportado
    previsível independentemente da taxa de bits do arquivo original.
    """
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
        duracao_segundos = obter_duracao_segundos(caminho_arquivo)

        precisa_dividir = (
            tamanho_bytes > WHISPER_LIMIT_BYTES or duracao_segundos > WHISPER_MAX_DURATION_SECONDS
        )

        if not precisa_dividir:
            texto = transcrever_chunk(caminho_arquivo)
        else:
            partes_por_tamanho = math.ceil(tamanho_bytes / CHUNK_TARGET_BYTES)
            partes_por_duracao = math.ceil(duracao_segundos / CHUNK_TARGET_DURATION_SECONDS)
            num_partes = max(1, partes_por_tamanho, partes_por_duracao)

            pasta_partes.mkdir(exist_ok=True)
            partes = dividir_audio_por_tamanho(caminho_arquivo, pasta_partes, num_partes)
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
