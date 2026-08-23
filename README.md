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
podcast-kb episodes                         # qué hay y en qué etapa está
podcast-kb process 1                        # descarga → WAV → Whisper → .md
podcast-kb bench tramo.wav                  # compara motores y modelos (§5.3)
```

### Requisitos para transcribir

`ffmpeg` y un motor de Whisper. En macOS:

```bash
brew install ffmpeg whisper-cpp

mkdir -p models && cd models
curl -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
curl -LO https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin
```

Para comparar contra `mlx-whisper` (§5.2): `uv pip install mlx-whisper`.

### Los tres benchmarks de la Fase 1

Deciden horas de cómputo del backfill, y se responden midiendo, no opinando.
Usa **un tramo de tertulia con gente hablando encima**, no un monólogo limpio:
es ahí donde `turbo` se degrada, y son dos de los tres podcasts.

```bash
ffmpeg -i episodio.mp3 -ss 00:12:00 -t 180 -ar 16000 -ac 1 -c:a pcm_s16le tramo.wav

# 1. turbo vs large-v3 (§15 TODO 3)
podcast-kb bench tramo.wav

# 2. whisper.cpp vs mlx-whisper (§15 TODO 2)
podcast-kb bench tramo.wav --engines whisper.cpp,mlx-whisper \
  --models models/ggml-large-v3-turbo-q5_0.bin

# 3. cuánto crece el JSON con timestamps por palabra (§15 TODO 1)
podcast-kb bench tramo.wav --word-timestamps
```

La velocidad la mide el comando. La **calidad la juzgas tú** con el criterio de
aceptación de §5.10: menos de 2 errores de glosario por tramo y 0 alucinaciones.

`config/podcasts.yaml` es la **fuente de verdad** del alta de podcasts; SQLite es caché
derivada más estado de ejecución. Si divergen, gana el YAML.

## Estado

**Fase 0 — cimientos**

- [x] Esqueleto del CLI, esquema SQLite, `config/podcasts.yaml`
- [x] Parseo de feeds con caché condicional (`ETag` / `If-Modified-Since`)
- [x] Deduplicación global por enclosure, que detecta el mismo audio en dos feeds
- [x] Detección de `<podcast:transcript>` y `<podcast:chapters>`
- [x] `sync --dry-run`
- [x] SQL del backend (`db/`), verificado contra Postgres 16
- [ ] **Resolver los 3 `feedUrl` reales** y validar el parseo contra ellos

**Fase 1 — camino completo sobre un episodio**

- [x] Descarga idempotente y normalización a 16 kHz mono PCM
- [x] whisper.cpp con VAD y glosario; mlx-whisper tras la misma interfaz
- [x] Post-proceso: eco del prompt, `fixups.tsv`, detector de bucles → `needs_review`
- [x] Emisión del `.md` + `.segments.json.gz` conforme a §7.2
- [x] `bench` para los tres TODOs que se deciden midiendo
- [ ] **Correr los benchmarks en el Mac** y fijar motor y modelo
- [ ] **Validación humana** con el criterio de §5.10

Fases siguientes: búsqueda funcionando con pocos episodios (2), decisión de
diarización antes del backfill (3), volumen (4).

## Desarrollo

```bash
uv run pytest
```

Los tests no tocan la red ni necesitan binarios: los feeds se sirven con
`httpx.MockTransport` sobre el fixture de `tests/fixtures/`, y `ffmpeg`,
`ffprobe` y `whisper-cli` se sustituyen por dobles que imitan su formato de
salida exacto. Eso cubre la construcción de los comandos y el parseo de sus
resultados, que es la parte que es nuestra.

## Estructura

```
config/     alta de feeds, glosarios, taxonomía, mapeo de hablantes
src/        podcast_kb: ingest → transcribe → enrich → export → index
db/         schema.sql, policies.sql (RLS), functions.sql (hybrid_search)
transcripts/  artefactos .md + .segments.json.gz (fuente de verdad)
docs/       diseño y revisión
```
