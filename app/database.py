import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "transcricao.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transcricoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome_arquivo TEXT NOT NULL,
            data_envio TEXT NOT NULL,
            tamanho_mb REAL NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('processando', 'concluido', 'erro')),
            texto_transcrito TEXT,
            mensagem_erro TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def criar_transcricao(nome_arquivo: str, data_envio: str, tamanho_mb: float) -> int:
    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO transcricoes (nome_arquivo, data_envio, tamanho_mb, status)
        VALUES (?, ?, ?, 'processando')
        """,
        (nome_arquivo, data_envio, tamanho_mb),
    )
    conn.commit()
    novo_id = cursor.lastrowid
    conn.close()
    return novo_id


def concluir_transcricao(id: int, texto_transcrito: str) -> None:
    conn = get_connection()
    conn.execute(
        """
        UPDATE transcricoes
        SET status = 'concluido', texto_transcrito = ?, mensagem_erro = NULL
        WHERE id = ?
        """,
        (texto_transcrito, id),
    )
    conn.commit()
    conn.close()


def marcar_erro(id: int, mensagem_erro: str) -> None:
    conn = get_connection()
    conn.execute(
        """
        UPDATE transcricoes
        SET status = 'erro', mensagem_erro = ?
        WHERE id = ?
        """,
        (mensagem_erro, id),
    )
    conn.commit()
    conn.close()


def listar_transcricoes() -> list[sqlite3.Row]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM transcricoes ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return rows


def obter_transcricao(id: int) -> Optional[sqlite3.Row]:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM transcricoes WHERE id = ?", (id,)
    ).fetchone()
    conn.close()
    return row
