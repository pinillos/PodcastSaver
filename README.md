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
podcast-kb add --slug X --apple-id 1723256857          # ID de Apple…
podcast-kb add --slug X --apple-id pod.link/1723256857 # …o la URL, da igual
podcast-kb add --slug X --rss https://...   # o directamente por URL
podcast-kb resolve --dry-run                # resuelve las feedUrl vía Apple y las valida
podcast-kb resolve                          # …y las escribe en el YAML
podcast-kb sync --dry-run                   # qué se detectaría, sin escribir ni descargar
podcast-kb sync --max-episodes 10           # alta real, en lotes acotados
podcast-kb episodes                         # qué hay y en qué etapa está
podcast-kb process 1                        # → .md (usa el subtítulo del feed si lo hay)
podcast-kb process 1 --force-whisper        # …o transcribe aunque lo haya
podcast-kb bench tramo.wav                  # compara motores y modelos (§5.3)
podcast-kb index --dry-run                  # cuántos chunks saldrían
podcast-kb index --dsn "postgresql://…"     # chunking + embeddings + carga
podcast-kb index --prune                    # …y quita del índice lo que ya no está
podcast-kb doctor                           # verifica el entorno
podcast-kb doctor --dsn "postgresql://…"    # …y el despliegue (RLS incluido)
```

Para el DSN, prefiere la variable `PODCAST_KB_DSN`: los argumentos de la línea de
órdenes son visibles para otros procesos.

```bash
```

### Resolver las feedUrl reales

```bash
uv run podcast-kb resolve --dry-run   # mira sin tocar nada
uv run podcast-kb resolve             # escribe rss_url en config/podcasts.yaml
```

Para cada podcast: pide el `feedUrl` a Apple, descarga el feed, comprueba que
parsea, y reporta cuántos episodios hay, desde cuándo, qué hosting está detrás
del tracking, y **si ya trae transcripciones publicadas** (§4.4) — que puede
ahorrar el backfill entero. Persiste la URL **final tras redirecciones**, no la
inicial (§2.3), y avisa si los autores que devuelve Apple no cuadran con los del
YAML, que es la trampa de §2.1.

Si Apple no devuelve `feedUrl`, el show es exclusivo de plataforma y no hay RSS.
Alternativas, por orden:

1. **pod.link/&lt;id&gt;** — usa el mismo identificador que Apple, así que si ya
   tienes la URL de pod.link puedes pasarla tal cual a `--apple-id`. Su página
   enlaza el RSS directamente.
2. **iVoox**: `https://www.ivoox.com/feed_fg_f<ID>_filtro_1.xml`, con el ID que
   aparece en la URL del podcast (`..._sq_f12364979_1.html` → `12364979`).
3. La web del podcast o su newsletter: casi siempre publican el enlace RSS.
4. `--rss` directo: `podcast-kb add --slug X --rss https://…`

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

- [x] Importación de subtítulos publicados por el feed (VTT/SRT), que evita
      descargar el audio y pasar Whisper en 128 episodios (148 h)

- [x] Descarga idempotente y normalización a 16 kHz mono PCM
- [x] whisper.cpp con VAD y glosario; mlx-whisper tras la misma interfaz
- [x] Post-proceso: eco del prompt, `fixups.tsv`, detector de bucles → `needs_review`
- [x] Emisión del `.md` + `.segments.json.gz` conforme a §7.2
- [x] `bench` para los tres TODOs que se deciden midiendo
- [ ] **Correr los benchmarks en el Mac** y fijar motor y modelo
- [ ] **Validación humana** con el criterio de §5.10

**Fase 2 — búsqueda funcionando**

- [x] Chunking de ~90 s con 20% de solape, alineado a los capítulos del autor
- [x] Interfaz de embeddings: `hashing` para desarrollo, e5/bge-m3 en local
- [x] Carga incremental por doble checksum, reutilizando vectores ya guardados
- [x] Esquema y RPC `hybrid_search` verificados contra Postgres 16
- [ ] **PWA de búsqueda** con `<audio>` y salto al segundo (§9)
- [x] Login por enlace mágico: sin sesión no hay búsqueda
- [x] `podcast-kb doctor`, que comprueba que el RLS está realmente aplicado
- [x] Revisión de seguridad: SSRF (incluido DNS rebinding), topes de descarga en
      streaming, recorrido de rutas, lista de acceso
- [ ] Fijar el modelo de embeddings y desplegar en Supabase

Fases siguientes: decisión de diarización antes del backfill (3), volumen (4).

## Desarrollo

```bash
uv run pytest              # la suite entera
uv run ruff check src tests
```

Hay CI: `.github/workflows/tests.yml` ejecuta lint, tests unitarios y tests de
navegador en cada push, y aplica `db/*.sql` contra un Postgres con pgvector para
comprobar que el esquema y el RPC siguen siendo válidos.

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
