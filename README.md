# podcast-kb

Base de conocimiento de transcripciones de podcasts de IA: sigue una lista de feeds RSS,
transcribe los episodios en local con Whisper, los publica como Markdown versionado y los
indexa con búsqueda híbrida para poder saltar al minuto exacto.

> **Repositorio privado.** Las transcripciones son obras derivadas de contenido con derechos
> de autor y el uso contemplado es personal. Ver §12 del diseño.

## Documentación

| Documento | Qué es |
|---|---|
| [`docs/diseno-v2.md`](docs/diseno-v2.md) | **Especificación vigente.** Contexto raíz para implementar. |
| [`docs/revision-diseno-v1.md`](docs/revision-diseno-v1.md) | Revisión de la v1: el porqué de cada cambio. |
| [`docs/diseno-v1.md`](docs/diseno-v1.md) | Diseño original. Superado, solo registro histórico. |

## Puesta en marcha

Requiere [`uv`](https://docs.astral.sh/uv/) (instala su propio Python en `~/.local`, sin
permisos de administrador).

```bash
uv sync
uv run podcast-kb init          # crea db/local.sqlite
uv run podcast-kb sync --dry-run
```

## Comandos

```bash
podcast-kb init                             # esquema SQLite local
podcast-kb add --slug X --apple-id 123456   # resuelve el feed vía Apple y lo añade al YAML
podcast-kb add --slug X --rss https://...   # o directamente por URL
podcast-kb sync --dry-run                   # qué se detectaría, sin escribir ni descargar
podcast-kb sync --max-episodes 10           # alta real, en lotes acotados
```

`config/podcasts.yaml` es la **fuente de verdad** del alta de podcasts; SQLite es caché
derivada más estado de ejecución. Si divergen, gana el YAML.

## Estado

Fase 0 (cimientos) del plan de §14 del diseño:

- [x] Esqueleto del CLI, esquema SQLite, `config/podcasts.yaml`
- [x] Parseo de feeds con caché condicional (`ETag` / `If-Modified-Since`)
- [x] Deduplicación global por enclosure, que detecta el mismo audio en dos feeds
- [x] Detección de `<podcast:transcript>` y `<podcast:chapters>`
- [x] `sync --dry-run`
- [x] SQL del backend (`db/`), verificado contra Postgres 16
- [ ] **Resolver los 3 `feedUrl` reales** y validar el parseo contra ellos

Fases siguientes: transcripción (1), búsqueda funcionando con pocos episodios (2),
diarización (3), backfill (4).

## Desarrollo

```bash
uv run pytest
```

Los tests no tocan la red: los feeds se sirven con `httpx.MockTransport` sobre el fixture de
`tests/fixtures/`.

## Estructura

```
config/     alta de feeds, glosarios, taxonomía, mapeo de hablantes
src/        podcast_kb: ingest → transcribe → enrich → export → index
db/         schema.sql, policies.sql (RLS), functions.sql (hybrid_search)
transcripts/  artefactos .md + .segments.json.gz (fuente de verdad)
docs/       diseño y revisión
```
