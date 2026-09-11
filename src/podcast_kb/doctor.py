"""Verificación del entorno y del despliegue (§14, Fase 5).

Comprueba lo que se puede olvidar y no avisa por sí solo. El caso crítico es
el RLS: si `db/policies.sql` no se aplica, las tablas quedan legibles con la
`anon key` y el corpus entero es público, en contra del §12.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import db as db_mod
from .paths import config_path, project_root

OK, AVISO, ERROR = "ok", "aviso", "error"


@dataclass
class Comprobacion:
    nombre: str
    estado: str
    detalle: str = ""


def _seguridad() -> list[Comprobacion]:
    """Cosas que dejan el sistema abierto sin dar ningún síntoma."""
    salida = []

    if os.environ.get("PODCAST_KB_ALLOW_PRIVATE_URLS") == "1":
        salida.append(
            Comprobacion(
                "PODCAST_KB_ALLOW_PRIVATE_URLS",
                AVISO,
                "activo: un feed podría hacer que pidas URLs internas (SSRF). "
                "Es para desarrollo y tests; apágalo fuera de ahí.",
            )
        )

    # Una cadena de conexión llega en dos formatos: `postgresql://…` (la que
    # copia la gente del panel de Supabase) y `host=… dbname=…`.
    def _parece_dsn(arg: str) -> bool:
        return arg.startswith(("postgres://", "postgresql://")) or (
            "dbname=" in arg and "host=" in arg
        )

    if any(_parece_dsn(a) for a in sys.argv):
        salida.append(
            Comprobacion(
                "--dsn en la línea de órdenes",
                AVISO,
                "los argumentos son visibles para otros procesos: usa PODCAST_KB_DSN.",
            )
        )

    gitignore = project_root() / ".gitignore"
    if gitignore.exists():
        contenido = gitignore.read_text(encoding="utf-8")
        faltan = [x for x in (".env", "config.json", "cache/") if x not in contenido]
        salida.append(
            Comprobacion("gitignore", OK, "cubre .env, config.json y cache/")
            if not faltan
            else Comprobacion("gitignore", ERROR, f"no ignora: {', '.join(faltan)}")
        )

    web_config = project_root() / "web" / "config.json"
    if web_config.exists():
        texto = web_config.read_text(encoding="utf-8")
        # La anon key es pública por diseño; la service_role NUNCA va al cliente.
        if "service_role" in texto or "SERVICE_ROLE" in texto:
            salida.append(
                Comprobacion(
                    "web/config.json",
                    ERROR,
                    "parece contener una service_role key. Esa clave salta el RLS "
                    "y el fichero lo sirve el navegador: rótala YA.",
                )
            )
        else:
            salida.append(Comprobacion("web/config.json", OK, "sin claves de servicio"))
    return salida


def _binarios() -> list[Comprobacion]:
    salida = []
    for nombre, para_que, critico in (
        ("ffmpeg", "normalizar audio", True),
        ("ffprobe", "medir duraciones", True),
        ("whisper-cli", "transcribir (whisper.cpp)", False),
        ("mlx_whisper", "transcribir (alternativa a medir)", False),
    ):
        ruta = shutil.which(nombre)
        if ruta:
            salida.append(Comprobacion(nombre, OK, ruta))
        else:
            salida.append(
                Comprobacion(nombre, ERROR if critico else AVISO, f"no está en el PATH ({para_que})")
            )
    return salida


def _configuracion() -> list[Comprobacion]:
    salida = []
    for fichero, critico in (
        ("podcasts.yaml", True),
        ("glossary.es.txt", False),
        ("fixups.tsv", False),
        ("topics.yaml", False),
    ):
        ruta = config_path(fichero)
        if ruta.exists():
            salida.append(Comprobacion(f"config/{fichero}", OK, str(ruta)))
        else:
            # Un glosario que falta degrada la transcripción sin dar error.
            salida.append(
                Comprobacion(f"config/{fichero}", ERROR if critico else AVISO, "no existe")
            )
    return salida


def _modelos() -> list[Comprobacion]:
    carpeta = project_root() / "models"
    if not carpeta.exists():
        return [Comprobacion("models/", AVISO, "no existe: descarga los .bin antes de transcribir")]
    ggml = sorted(carpeta.glob("ggml-*.bin"))
    if not ggml:
        return [Comprobacion("models/", AVISO, "sin modelos ggml-*.bin")]
    return [
        Comprobacion("models/", OK, ", ".join(f"{m.name} ({m.stat().st_size // 2**20} MB)" for m in ggml))
    ]


def _estado_local(db_path: str | Path) -> list[Comprobacion]:
    ruta = Path(db_path)
    if not ruta.exists():
        return [Comprobacion("SQLite", AVISO, f"{ruta} no existe: corre `podcast-kb init`")]
    conn = db_mod.connect(ruta)
    faltan = db_mod.migrate(conn)
    fila = conn.execute(
        "select count(*) n, sum(case when stage='exported' then 1 else 0 end) e,"
        " sum(needs_review) r from episodes"
    ).fetchone()
    salida = [
        Comprobacion(
            "SQLite",
            OK,
            f"{fila['n'] or 0} episodios, {fila['e'] or 0} exportados, "
            f"{fila['r'] or 0} marcados para revisión",
        )
    ]
    if faltan:
        salida.append(Comprobacion("migraciones", OK, f"aplicadas: {', '.join(faltan)}"))
    return salida


TABLAS = ("podcasts", "episodes", "chunks")


def comprobar_backend(dsn: str) -> list[Comprobacion]:
    """Lo que hay que verificar del despliegue en Postgres."""
    try:
        import psycopg
    except ImportError:
        return [Comprobacion("Postgres", AVISO, "falta psycopg: no se pudo comprobar")]

    salida: list[Comprobacion] = []
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            cur = conn.cursor()

            # 1. RLS. Es la comprobación importante: sin él, el corpus es
            #    público para cualquiera con la anon key (§7.5).
            cur.execute(
                "select relname, relrowsecurity from pg_class "
                "where relname = any(%s) and relkind = 'r'",
                (list(TABLAS),),
            )
            encontrado = dict(cur.fetchall())
            sin_rls = [t for t in TABLAS if encontrado.get(t) is False]
            faltan = [t for t in TABLAS if t not in encontrado]
            if faltan:
                salida.append(Comprobacion("esquema", ERROR, f"faltan tablas: {', '.join(faltan)}"))
            if sin_rls:
                salida.append(
                    Comprobacion(
                        "RLS",
                        ERROR,
                        f"SIN row level security en {', '.join(sin_rls)}: el corpus es "
                        "PÚBLICO para cualquiera con la anon key. Aplica db/policies.sql.",
                    )
                )
            elif not faltan:
                salida.append(Comprobacion("RLS", OK, "activo en las tres tablas"))

            # 2. pgvector y el tipo de la columna.
            cur.execute("select extversion from pg_extension where extname = 'vector'")
            fila = cur.fetchone()
            if fila:
                salida.append(Comprobacion("pgvector", OK, f"v{fila[0]}"))
                if tuple(int(x) for x in fila[0].split(".")[:2]) < (0, 8):
                    salida.append(
                        Comprobacion(
                            "hnsw.iterative_scan",
                            AVISO,
                            "requiere pgvector >= 0.8; sin él, filtrar destruye el recall (B.1)",
                        )
                    )
            else:
                salida.append(Comprobacion("pgvector", ERROR, "la extensión no está instalada"))

            # 3. Índices.
            cur.execute("select indexname from pg_indexes where tablename = 'chunks'")
            indices = {r[0] for r in cur.fetchall()}
            for necesario in ("chunks_tsv_es_idx", "chunks_tsv_en_idx", "chunks_embedding_idx"):
                salida.append(
                    Comprobacion(necesario, OK, "presente")
                    if necesario in indices
                    else Comprobacion(necesario, ERROR, "falta")
                )

            # 4. El RPC.
            cur.execute("select 1 from pg_proc where proname = 'hybrid_search'")
            salida.append(
                Comprobacion("hybrid_search", OK, "presente")
                if cur.fetchone()
                else Comprobacion("hybrid_search", ERROR, "falta: aplica db/functions.sql")
            )

            # 5. Contenido.
            cur.execute("select count(*) from chunks")
            salida.append(Comprobacion("chunks indexados", OK, str(cur.fetchone()[0])))
            cur.execute("select count(*) from chunks where embedding is null")
            sin_vector = cur.fetchone()[0]
            if sin_vector:
                salida.append(
                    Comprobacion("embeddings", AVISO, f"{sin_vector} chunks sin vector")
                )
    except Exception as exc:
        salida.append(Comprobacion("Postgres", ERROR, f"{type(exc).__name__}: {exc}"))
    return salida


def comprobar_local(db_path: str | Path) -> list[Comprobacion]:
    return _seguridad() + _binarios() + _configuracion() + _modelos() + _estado_local(db_path)
