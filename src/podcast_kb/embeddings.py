"""Embeddings para la mitad semántica del índice (§8.2).

El modelo debe ser multilingüe: con corpus mixto es/en, uno monolingüe hace
que una consulta en español no recupere nada de los episodios en inglés.

`multilingual-e5-large` y `bge-m3` son ambos de 1024 dimensiones, así que el
esquema no depende de cuál se elija y la decisión puede aplazarse (§15).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from .paths import project_root

DIMENSIONS = 1024

# e5 exige prefijos distintos para consulta y pasaje. Usar uno sin el otro
# degrada el recall sin dar ningún síntoma.
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "


class EmbeddingError(RuntimeError):
    pass


@dataclass
class Embedder:
    """Interfaz común. `name` va al índice para saber qué generó cada vector."""

    name: str
    dimensions: int = DIMENSIONS

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError


class HashingEmbedder(Embedder):
    """Embedder determinista, sin dependencias, para pruebas y desarrollo.

    NO sirve para buscar: no captura significado. Existe para poder ejercitar
    todo el camino de indexado y el RPC sin descargar 2 GB de modelo, y para
    que los tests no dependan de la red.
    """

    def __init__(self, dimensions: int = DIMENSIONS) -> None:
        super().__init__(name="hashing-dev", dimensions=dimensions)

    def _vector(self, text: str) -> list[float]:
        out: list[float] = []
        counter = 0
        while len(out) < self.dimensions:
            digest = hashlib.sha256(f"{counter}:{text}".encode("utf-8")).digest()
            for i in range(0, len(digest) - 3, 4):
                if len(out) >= self.dimensions:
                    break
                (value,) = struct.unpack_from(">I", digest, i)
                out.append((value / 0xFFFFFFFF) * 2 - 1)
            counter += 1
        norm = sum(v * v for v in out) ** 0.5 or 1.0
        return [v / norm for v in out]

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(PASSAGE_PREFIX + t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(QUERY_PREFIX + text)


class SentenceTransformerEmbedder(Embedder):
    """`multilingual-e5-large` o `bge-m3` en local (§8.2)."""

    def __init__(self, model_name: str = "intfloat/multilingual-e5-large") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise EmbeddingError(
                "Falta sentence-transformers. Instálalo con "
                "`uv pip install sentence-transformers`, o usa --embedder hashing "
                "para probar el camino sin modelo."
            ) from exc

        cache = project_root() / "models" / "embeddings"
        self._model = SentenceTransformer(model_name, cache_folder=str(cache))
        dims = self._model.get_sentence_embedding_dimension()
        super().__init__(name=model_name, dimensions=dims)

        if dims != DIMENSIONS:
            raise EmbeddingError(
                f"{model_name} produce {dims} dimensiones y el esquema espera "
                f"{DIMENSIONS}. Cambiar de modelo implica migrar la columna y "
                "reembeder (barato: se rehace desde los .md)."
            )

    # e5 necesita los prefijos; bge-m3 los ignora sin penalización.
    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            [PASSAGE_PREFIX + t for t in texts], normalize_embeddings=True
        )
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self._model.encode(
            QUERY_PREFIX + text, normalize_embeddings=True
        ).tolist()


def get_embedder(name: str = "hashing") -> Embedder:
    if name in ("hashing", "dev"):
        return HashingEmbedder()
    if name in ("e5", "multilingual-e5-large"):
        return SentenceTransformerEmbedder("intfloat/multilingual-e5-large")
    if name in ("bge-m3", "bge"):
        return SentenceTransformerEmbedder("BAAI/bge-m3")
    raise EmbeddingError(f"Embedder desconocido: {name!r}")


def to_pgvector(vector: list[float]) -> str:
    """pgvector acepta el literal `[1,2,3]` como texto."""
    return "[" + ",".join(f"{v:.7g}" for v in vector) + "]"
