---
proyecto: podcast-kb
titulo: "Base de conocimiento de transcripciones de podcasts de IA"
version: 1.0
fecha: 2026-08-21
idioma_doc: es
estado: especificación funcional y técnica (pre-código)
---

# Proyecto: Base de conocimiento de transcripciones de podcasts

> **⚠️ DOCUMENTO SUPERADO.** Esta es la v1, conservada solo como registro
> histórico. La especificación vigente es `docs/diseno-v2.md`; el porqué de cada
> cambio está en `docs/revision-diseno-v1.md`. **No implementar a partir de este
> fichero**: contiene al menos un error de flag (`-ml 1`, §5.4) y un esquema sin
> RLS que publicaría el corpus.

> **Propósito de este documento**: servir de entrada única (contexto raíz) para un agente de código
> que vaya a implementar el sistema. Contiene objetivos, arquitectura, contratos de datos,
> esquemas, decisiones tomadas y decisiones pendientes. Todo lo marcado como `TODO`
> requiere confirmación humana antes de implementarse.

---

## 1. Objetivo

Construir un sistema personal que:

1. Siga una lista **extensible** de podcasts (principalmente sobre IA, en español con anglicismos, y potencialmente en inglés).
2. Descargue automáticamente los episodios nuevos desde sus **feeds RSS públicos**.
3. Los transcriba a texto con **timestamps**, en local y sin coste por minuto.
4. Enriquezca cada transcripción con metadatos derivados (resumen, temas, entidades, palabras clave).
5. Los publique como **ficheros Markdown** (fuente de verdad, versionables).
6. Los indexe en un backend con **búsqueda híbrida** (léxica + semántica).
7. Permita buscar y **saltar al minuto exacto** del episodio relevante, desde escritorio o móvil.

### 1.1 No-objetivos (fuera de alcance v1)

- No se usa la API de Spotify: **Spotify no expone transcripciones por API pública**. Toda la ingesta parte del RSS original.
- No se re-publican las transcripciones públicamente (ver §12, consideraciones legales).
- No se hace transcripción en el propio Android (inviable para volumen; ver §4.3).
- No se construye reproductor propio en v1: se hace *deep-link* al audio original.

---

## 2. Fuentes iniciales

Tres podcasts confirmados. **Todos tienen distribución fuera de Spotify**, luego tienen feed RSS accesible.

| # | Podcast | Autores | Idioma | Pistas de feed encontradas | Estado |
|---|---------|---------|--------|----------------------------|--------|
| 1 | Inteligencia Artificial Semanal | Gargoyles Devon (`gargoylesdevon@gmail.com`) | es | `https://pod.link/1723256857` · iVoox `f12364979` · YouTube `UC4ewDq9cCPd9eqA88M6yvEQ` · Spotify show `48stHRcIdLoDi8WPK8JJWq` | Feed a resolver desde pod.link / Apple ID `1723256857` |
| 2 | El Test de Turing — IA Aplicada a Negocio | Álvaro Peña, Arnau Vendrell, Víctor Mollá | es | `https://anchor.fm/s/e1671d44/podcast/rss` · Apple `id1771978939` · YouTube `@ElTestdeTuring` | **Feed RSS directo identificado** |
| 3 | La Tertul-IA: Inteligencia Artificial y más | Lu Martín, Frankie Carrero, Corti (Product Hackers / VASS) | es | Apple `id1723736263` · Spotify show `2trIE4WvCf3AWzNaLodxC4` · newsletter `tertulia.mumbler.io` | Feed a resolver desde Apple ID |

### 2.1 Aviso de desambiguación

Existe un podcast distinto y homónimo: **"La TERTULia de la Inteligencia Artificial"** (ironbar.github.io, Apple `id1669083682`, Spotify `2yxHFbLvZC16ZV8Of7I7qH`). **No es el mismo** que el #3. Verificar por autores antes de dar de alta el feed.

### 2.2 Resolución de feed a partir de un ID de Apple Podcasts

```
GET https://itunes.apple.com/lookup?id=<APPLE_ID>&entity=podcast
→ results[0].feedUrl
```

Esta es la vía canónica para dar de alta un podcast cuando solo se conoce su enlace de Apple o de `pod.link` (que usa el mismo ID). Debe implementarse como utilidad del CLI (`podcast add --apple-id ...`).

---

## 3. Arquitectura general

Sistema **desacoplado en dos mitades** por restricciones de equipo y conectividad:

```
┌───────────────────────────────┐        ┌──────────────────────────────┐
│   MITAD LOCAL (MacBook Air)   │        │   MITAD REMOTA (nube)        │
│                               │        │                              │
│  1. Ingesta RSS               │        │  Git repo (Markdown)         │
│  2. Descarga de audio         │  push  │        ↓                     │
│  3. Speech-to-text (whisper)  │ ─────► │  Supabase / Postgres         │
│  4. Enriquecimiento (opc.)    │   MD   │    · tsvector (FTS)          │
│  5. Emisión de ficheros .md   │        │    · pgvector (embeddings)   │
│                               │        │        ↓                     │
│  Estado local: SQLite         │        │  Búsqueda híbrida + UI web   │
└───────────────────────────────┘        └──────────────────────────────┘
                                                     ▲
                                                     │ consulta
                                                Android / navegador
```

**Principio rector**: el Mac hace solo lo pesado y produce **artefactos de texto plano**.
Todo lo demás (indexado, embeddings, búsqueda, UI) ocurre aguas abajo y puede rehacerse
desde cero en cualquier momento a partir de los `.md`, sin volver a transcribir nada.

### 3.1 Consecuencias del desacople

- Los ficheros `.md` son **idempotentes y reproducibles**: reindexar es barato, retranscribir es caro.
- El Mac puede estar offline durante horas; sincroniza cuando puede.
- La transcripción se puede migrar a otra máquina sin tocar el backend.

---

## 4. Etapa 1 — Ingesta RSS

### 4.1 Registro de podcasts

Tabla `podcasts` (local, SQLite) con los campos de §7.1. Alta manual vía CLI:

```bash
podcast-kb add --rss "https://anchor.fm/s/e1671d44/podcast/rss" --lang es --slug test-de-turing
podcast-kb add --apple-id 1723736263 --lang es --slug la-tertul-ia
```

### 4.2 Detección de episodios nuevos (deduplicación)

Clave de identidad de un episodio, por orden de preferencia:

1. `<guid>` del ítem RSS (estable por especificación; **usar siempre que exista**).
2. Si falta o no es estable: `sha256(enclosure_url_normalizada)`.
3. Fallback: `sha256(titulo_normalizado + fecha_publicacion)`.

Reglas:

- Se persiste `guid` con `UNIQUE` en la tabla `episodes`. Un `INSERT OR IGNORE` basta para deduplicar entre ejecuciones.
- Se usan cabeceras condicionales `ETag` / `If-Modified-Since` al pedir el feed, para no descargarlo entero cada vez.
- Máquina de estados por episodio: `discovered → downloaded → transcribed → enriched → exported → published`.
  Cada transición se persiste con timestamp, de modo que una ejecución interrumpida se retoma exactamente donde se quedó.
- Los reintentos son idempotentes: si un fichero de audio ya existe con el tamaño esperado, no se vuelve a descargar.

### 4.3 Planificación

- Ejecución manual (`podcast-kb sync`) y/o `launchd` en macOS (preferible a `cron` en portátiles, porque respeta suspensiones y recupera ejecuciones perdidas).
- Cadencia sugerida: diaria. Los tres podcasts son semanales.

---

## 5. Etapa 2 — Speech-to-text

### 5.1 Decisión: whisper local, coste cero

Whisper es open source y se ejecuta en local **sin coste por minuto**. La API de OpenAI (≈0,006 USD/min, ~0,36 USD/hora de audio) queda solo como plan B para episodios problemáticos.

### 5.2 Entorno objetivo: MacBook Air (Apple Silicon)

- **Implementación recomendada: `whisper.cpp`**, por dos motivos:
  1. Es un binario compilado sin dependencias pesadas de Python → encaja con un equipo corporativo donde no se puede instalar libremente.
  2. Soporta **aceleración Metal**, aprovechando la GPU integrada del chip Apple Silicon.
- Alternativa si se permite Python: `faster-whisper` (backend CTranslate2), también viable en CPU.
- **Modelo por defecto: `medium`**. Orden de calidad/coste: `small` < `medium` < `large-v3`.
  Para español técnico, `medium` suele ser el punto dulce; `large-v3` si la calidad no basta.
- Formato de modelo para whisper.cpp: `ggml-medium.bin` (cuantizado `q5_0` si se quiere reducir RAM).

**Consideración térmica**: el Air es *fanless*. En lotes largos hay *throttling*. Recomendaciones:
procesar en lotes acotados, permitir `--max-episodes N` por ejecución, y no encadenar
decenas de episodios seguidos sin pausa.

### 5.3 Preprocesado de audio

Normalizar siempre antes de transcribir:

```bash
ffmpeg -i input.mp3 -ar 16000 -ac 1 -c:a pcm_s16le output.wav
```

(16 kHz, mono, PCM 16-bit: formato nativo de whisper.cpp.)

### 5.4 Idioma — decisión clave

**No fijar un idioma global.** El idioma es un **campo por podcast** (`podcasts.language`), y se pasa por episodio:

```bash
whisper-cli -m models/ggml-medium.bin -f ep.wav -l es --prompt "<glosario>" -oj -ml 1
```

Motivo: el corpus mezclará podcasts en español y en inglés. Un `--language` global degradaría unos u otros. La autodetección (`-l auto`) se evita porque en español-con-muchos-anglicismos puede oscilar dentro del mismo episodio.

### 5.5 Anglicismos — glosario vía *initial prompt*

Whisper acepta un prompt inicial que condiciona el vocabulario. Es la palanca más efectiva
para que los tecnicismos en inglés se transcriban bien dentro de audio en español.

Glosario base sugerido (mantener en `config/glossary.<lang>.txt`, editable):

```
embeddings, prompt, prompting, fine-tuning, tokens, context window, RAG,
transformer, attention, benchmark, open source, deploy, inference, dataset,
agentes, agentic, MCP, LLM, GPT, Claude, Gemini, Llama, Mistral, DeepSeek,
OpenAI, Anthropic, Hugging Face, NVIDIA, Cursor, Copilot, quantization,
reasoning, chain of thought, hallucination, guardrails, vibe coding
```

Notas de implementación:
- El prompt tiene un límite (~224 tokens). Si el glosario crece, **priorizar por frecuencia** o rotarlo por podcast.
- Permitir un glosario específico por podcast que se concatene al global.
- El glosario es *soft*: no garantiza la grafía. Complementar con un **post-procesado de normalización** (mapa de sustituciones regex, p.ej. `fain tuning → fine-tuning`), auditable en `config/fixups.tsv`.

### 5.6 Salida requerida

Salida en **JSON con segmentos y timestamps** (`-oj`), no texto plano. Cada segmento:
`{start, end, text}`. Los timestamps son la base de todo el valor del sistema (§9).

### 5.7 Diarización (quién habla) — recomendado, no bloqueante

Dos de los tres podcasts son tertulias a 3 voces. Separar hablantes mejora notablemente
la calidad de búsqueda ("qué dijo Corti sobre X").

- Whisper **no diariza**. Requiere una pasada adicional: `pyannote.audio` (requiere token de Hugging Face y aceptar licencia del modelo) o `whisperX` (alinea + diariza).
- **Decisión**: dejarlo para **fase 2**. El esquema de datos ya reserva `speaker` en el segmento (nullable) para no requerir migración después.
- Si se implementa: mapear etiquetas genéricas (`SPEAKER_00`) a nombres reales mediante un mapa por podcast, ya que el orden no es estable entre episodios.

---

## 6. Etapa 3 — Enriquecimiento

Por cada transcripción completa, generar con un LLM:

| Campo | Descripción |
|-------|-------------|
| `summary` | Resumen de 3–5 frases. |
| `topics[]` | 5–10 temas normalizados (taxonomía controlada, ver abajo). |
| `entities[]` | Modelos, empresas, personas, productos mencionados. |
| `keywords[]` | Términos para el índice léxico. |
| `chapters[]` | Secciones con `start_time` (muchos episodios ya traen capítulos en las *show notes* del RSS → **preferir los del autor si existen**). |

Notas:
- Mantener una **taxonomía controlada** en `config/topics.yaml` y pedir al modelo que se ciña a ella, con un cajón `otros` que se revisa periódicamente. Sin esto, los temas divergen y el filtrado por facetas se vuelve inútil.
- Para episodios largos, resumir por *chunks* y luego consolidar (map-reduce).
- Esta etapa es **opcional para la v1**: el sistema es útil sin ella. Diseñarla como paso independiente y re-ejecutable sobre los `.md` ya existentes.
- Aprovechar la descripción del ítem RSS (`<description>` / `<itunes:summary>`) como metadato de origen, sin coste de LLM.

---

## 7. Contratos de datos

### 7.1 Estado local (SQLite, en el Mac)

```sql
CREATE TABLE podcasts (
  id             INTEGER PRIMARY KEY,
  slug           TEXT NOT NULL UNIQUE,     -- 'test-de-turing'
  title          TEXT NOT NULL,
  authors        TEXT,
  rss_url        TEXT NOT NULL UNIQUE,
  apple_id       TEXT,
  spotify_show_id TEXT,
  website        TEXT,
  language       TEXT NOT NULL,            -- 'es' | 'en'  (§5.4)
  glossary_path  TEXT,                     -- glosario específico opcional
  active         INTEGER NOT NULL DEFAULT 1,
  etag           TEXT,                     -- caché condicional del feed
  last_modified  TEXT,
  last_synced_at TEXT
);

CREATE TABLE episodes (
  id             INTEGER PRIMARY KEY,
  podcast_id     INTEGER NOT NULL REFERENCES podcasts(id),
  guid           TEXT NOT NULL,            -- §4.2
  title          TEXT NOT NULL,
  episode_number INTEGER,                  -- si se puede extraer del título
  published_at   TEXT NOT NULL,            -- ISO 8601
  duration_sec   INTEGER,
  audio_url      TEXT NOT NULL,
  audio_bytes    INTEGER,
  description    TEXT,                     -- show notes del RSS
  episode_url    TEXT,                     -- página del episodio
  language       TEXT,                     -- hereda de podcasts, override posible
  status         TEXT NOT NULL,            -- máquina de estados §4.2
  local_audio    TEXT,                     -- ruta si está cacheado
  md_path        TEXT,                     -- ruta del artefacto exportado
  transcript_model TEXT,                   -- 'whisper.cpp/ggml-medium'
  transcribed_at TEXT,
  error          TEXT,
  UNIQUE (podcast_id, guid)
);
```

### 7.2 Artefacto de salida: fichero Markdown con front matter

**Un fichero por episodio.** Es la fuente de verdad y el contrato entre las dos mitades del sistema.

Ruta: `transcripts/<podcast_slug>/<YYYY-MM-DD>-<episode-slug>.md`

```markdown
---
schema_version: 1
podcast: "El Test de Turing - IA Aplicada a Negocio"
podcast_slug: test-de-turing
authors: ["Álvaro Peña", "Arnau Vendrell", "Víctor Mollá"]
episode_title: "Agentes IA: Destripando los enigmas con un PRO"
episode_number: 121
guid: "e1671d44-xxxx-xxxx"
published_at: "2026-07-15T06:00:00Z"
duration_sec: 3782
language: es
audio_url: "https://anchor.fm/s/e1671d44/podcast/play/..../episode.mp3"
episode_url: "https://open.spotify.com/episode/17erE3notlcJ3pyFXqY3YW"
transcript:
  engine: "whisper.cpp"
  model: "ggml-medium"
  transcribed_at: "2026-08-21T10:22:00Z"
  glossary: "es-tech-v1"
  diarized: false
enrichment:
  summary: "..."
  topics: ["agentes", "herramientas-dev", "frameworks"]
  entities: ["Claude Code", "CodeGPT", "Daniel Ávila"]
  keywords: ["agente", "terminal", "in-house"]
chapters:
  - { start: 0,    title: "Intro y presentación" }
  - { start: 62,   title: "Inteligencia artificial y SEO" }
source: rss
checksum_audio: "sha256:..."
---

# Agentes IA: Destripando los enigmas con un PRO (Ep. 121)

## Transcripción

[00:00:00] Texto del primer segmento agrupado...

[00:01:12] Siguiente bloque...

[00:02:45] (Frankie) Con diarización, el hablante va así.
```

**Reglas del formato:**

- Front matter **YAML**, no TOML ni JSON, por legibilidad y compatibilidad con Obsidian.
- Los segmentos crudos de whisper (a menudo de 3–8 s) se **agrupan en bloques de ~30–60 s** para legibilidad humana. El timestamp del bloque es el `start` del primer segmento.
- **Conservar los segmentos crudos aparte**, en `transcripts/<slug>/<fichero>.segments.json`, para poder rehacer el *chunking* sin retranscribir. El `.md` es para humanos y para indexar; el JSON es la materia prima.
- Timestamps en formato `[HH:MM:SS]`, siempre absolutos desde el inicio del audio.
- `schema_version` explícito, para poder migrar el formato en el futuro.
- Nombres de fichero: ASCII, minúsculas, sin espacios (slug), para evitar problemas entre macOS/Git/Linux.

### 7.3 Backend (Supabase / Postgres)

```sql
create table podcasts (
  id uuid primary key default gen_random_uuid(),
  slug text unique not null,
  title text not null,
  authors text[],
  language text not null,
  rss_url text,
  website text
);

create table episodes (
  id uuid primary key default gen_random_uuid(),
  podcast_id uuid not null references podcasts(id) on delete cascade,
  guid text not null,
  title text not null,
  episode_number int,
  published_at timestamptz not null,
  duration_sec int,
  audio_url text not null,
  episode_url text,
  language text not null,
  summary text,
  topics text[],
  entities text[],
  keywords text[],
  chapters jsonb,
  md_checksum text,               -- para reindexar solo lo que cambió
  indexed_at timestamptz,
  unique (podcast_id, guid)
);

create table chunks (
  id bigserial primary key,
  episode_id uuid not null references episodes(id) on delete cascade,
  idx int not null,               -- orden dentro del episodio
  start_sec int not null,
  end_sec int not null,
  speaker text,                   -- reservado para diarización (§5.7)
  content text not null,
  tsv tsvector,                   -- índice léxico
  embedding vector(1024),         -- índice semántico (§8.2)
  unique (episode_id, idx)
);

-- Índice léxico
create index chunks_tsv_idx on chunks using gin (tsv);

-- Índice vectorial (HNSW; requiere pgvector >= 0.5)
create index chunks_embedding_idx on chunks
  using hnsw (embedding vector_cosine_ops);

create index episodes_published_idx on episodes (published_at desc);
create index episodes_topics_idx on episodes using gin (topics);
```

Sobre `tsvector` y el idioma: Postgres necesita una configuración de idioma
(`spanish` / `english`) para el *stemming*. Al ser corpus mixto, poblar `tsv` con la
configuración correspondiente al idioma del episodio:

```sql
-- en la ingesta, según episodes.language
tsv = to_tsvector('spanish', content)   -- o 'english'
```

Y consultar con la misma configuración, o con ambas y fusionar. Alternativa más simple y
robusta para corpus mixto: usar la configuración `simple` (sin stemming) y confiar el
matching morfológico a la mitad semántica. **Decisión sugerida: `spanish`/`english` por
episodio**, ya que el stemming aporta mucho en consultas léxicas cortas.

---

## 8. Etapa 4 — Indexación híbrida

### 8.1 Chunking

- Unidad de indexado: **fragmento de ~1–2 minutos de audio** (aprox. 150–300 palabras en español hablado).
- **Solape del 15–20%** entre fragmentos consecutivos, para no partir una idea a la mitad.
- Respetar fronteras de segmento de whisper: nunca cortar a mitad de frase.
- Si hay capítulos, alinear preferentemente las fronteras de chunk con las de capítulo.
- Cada chunk conserva `start_sec` / `end_sec`: es lo que permite el salto temporal (§9).

### 8.2 Embeddings — decisión crítica

**El modelo de embeddings debe ser multilingüe.** Con un corpus mixto español/inglés, un
modelo monolingüe hace que una consulta en español no recupere nada de los episodios en
inglés, y viceversa.

Opciones (elegir una y fijarla en `config`):

| Modelo | Dim. | Notas |
|--------|------|-------|
| `multilingual-e5-large` | 1024 | Open source, local, muy sólido en es/en. Requiere prefijos `query:` / `passage:`. |
| `bge-m3` | 1024 | Open source, multilingüe, soporta también recuperación léxica. |
| `voyage-3` / `text-embedding-3-large` | var. | API de pago, cero infraestructura. |

**Recomendación por defecto: `multilingual-e5-large`**, ejecutable en local en el Mac
durante la fase de exportación, o en el backend durante la ingesta. La dimensión del
esquema (`vector(1024)`) asume esta elección; cambiar de modelo implica migración de columna
y reembedding completo (barato: se rehace desde los `.md`).

### 8.3 Fusión de resultados

Ejecutar ambas búsquedas en paralelo y fusionar con **Reciprocal Rank Fusion (RRF)**:

```
score(d) = Σ_i  1 / (k + rank_i(d))        con k ≈ 60
```

RRF es preferible a la suma ponderada de scores porque no requiere normalizar escalas
heterogéneas (`ts_rank` vs. distancia coseno) ni calibrar pesos.

Implementar como función RPC en Supabase (`hybrid_search(query, lang, filters, limit)`)
para que la UI haga una sola llamada.

### 8.4 Filtros y facetas

La búsqueda debe aceptar: podcast(s), rango de fechas, idioma, `topics[]`, `speaker`.
Ejemplo de intención a soportar: *"menciones a MCP en El Test de Turing durante 2026"*.

### 8.5 Reindexado incremental

Guardar `md_checksum` por episodio. En cada ingesta, comparar; solo reprocesar los `.md`
cuyo checksum haya cambiado. Permitir `--force` para reindexado completo.

---

## 9. Acceso al audio y salto temporal

**Decisión: no duplicar los MP3 por defecto.** Se persiste `audio_url` del enclosure RSS y
se abre con salto al segundo exacto.

- Enlace directo con fragmento temporal: `https://.../episode.mp3#t=<segundos>`
  (soportado por navegadores y por la mayoría de reproductores nativos).
- Enlace alternativo a la plataforma (Spotify / YouTube) como secundario, para escucha cómoda.
- **Riesgo asumido**: si el hosting cambia o retira la URL, se pierde el acceso al audio
  (la transcripción sobrevive). Mitigación: **caché opcional** de los episodios más
  consultados o marcados como favoritos, en almacenamiento local o en un bucket
  (Supabase Storage). Implementar como opción, no por defecto.
- Guardar `checksum_audio` para poder detectar re-subidas o ediciones del episodio.

---

## 10. Elección de almacenamiento (decisiones tomadas)

| Capa | Elección | Motivo |
|------|----------|--------|
| Fuente de verdad | **Git + Markdown** | Texto plano, versionable, diff legible, portable, sin *lock-in*. |
| Índice y búsqueda | **Supabase (Postgres)** | FTS (`tsvector`) y `pgvector` en el mismo motor → índice híbrido sin sincronizar dos sistemas. API REST/RPC lista para consumir desde móvil. |
| Lectura y navegación manual | **Obsidian** (opcional) | Apunta al mismo directorio del repo. Excelente para leer y enlazar a mano; **no** se usa como motor de búsqueda. |
| Estado del pipeline local | **SQLite** | Cero configuración, transaccional, vive junto al proceso que lo usa. |

Descartado: mantener el índice en SQLite+FTS5+`sqlite-vec` en el Mac. Es una alternativa
válida y muy ligera **si se prefiere una solución 100% local**, pero rompe el requisito de
consultar desde Android sin depender del portátil encendido.

---

## 11. Estructura del repositorio

```
podcast-kb/
├── README.md
├── config/
│   ├── podcasts.yaml          # alta declarativa de feeds (fuente para SQLite)
│   ├── glossary.es.txt
│   ├── glossary.en.txt
│   ├── fixups.tsv             # normalización post-whisper
│   └── topics.yaml            # taxonomía controlada
├── src/
│   ├── ingest/                # etapa 1: RSS
│   ├── transcribe/            # etapa 2: whisper.cpp
│   ├── enrich/                # etapa 3: LLM
│   ├── export/                # etapa 4a: emisión de .md
│   └── index/                 # etapa 4b: ingesta en Supabase
├── transcripts/
│   ├── test-de-turing/
│   │   ├── 2026-07-15-agentes-ia-destripando-los-enigmas.md
│   │   └── 2026-07-15-agentes-ia-destripando-los-enigmas.segments.json
│   ├── ia-semanal/
│   └── la-tertul-ia/
├── db/
│   ├── schema.sql             # Postgres (Supabase)
│   ├── functions.sql          # hybrid_search RPC
│   └── local.sqlite           # (gitignored)
├── models/                    # (gitignored) ggml-*.bin
├── cache/audio/               # (gitignored)
└── web/                       # UI de búsqueda
```

`.gitignore` debe excluir: `models/`, `cache/`, `db/local.sqlite`, `.env`.

---

## 12. Consideraciones legales y de privacidad

- Las transcripciones son **obras derivadas** de contenido con derechos de autor. El uso
  contemplado aquí es **personal y privado** (indexado y búsqueda para consumo propio).
- **No publicar** las transcripciones completas en abierto sin permiso de los autores.
  Si en algún momento se quisiera una UI pública, limitar a fragmentos cortos, atribuir
  explícitamente al podcast y enlazar siempre al episodio original.
- Respetar el RSS como interfaz prevista de distribución: identificarse con un `User-Agent`
  propio, aplicar *rate limiting* y respetar `retry-after` / `429`.
- Si se usa Supabase, los datos salen del equipo local: **no** volcar ahí material corporativo
  ni credenciales. El proyecto solo maneja contenido público de podcasts.
- Si la transcripción se ejecuta en un equipo de empresa, verificar que el uso de recursos y
  la instalación del binario cumplen la política interna.

---

## 13. Plan de implementación por fases

### Fase 0 — Cimientos
- [ ] Resolver los 3 `feedUrl` reales vía `itunes.apple.com/lookup` y validar el parseo.
- [ ] Esqueleto del CLI + esquema SQLite + `config/podcasts.yaml`.
- [ ] `podcast-kb sync --dry-run`: lista episodios detectados sin descargar nada.

### Fase 1 — Camino completo sobre 1 episodio
- [ ] Descarga de audio + normalización con ffmpeg.
- [ ] whisper.cpp con Metal, modelo `medium`, glosario y `-oj`.
- [ ] Emisión del `.md` + `.segments.json` conforme a §7.2.
- [ ] **Validación humana de la calidad de transcripción** antes de escalar. Ajustar modelo/glosario aquí.

### Fase 2 — Volumen
- [ ] Backfill del histórico de los 3 podcasts (lotes acotados, §5.2).
- [ ] Post-procesado con `fixups.tsv`.
- [ ] Enriquecimiento con LLM.

### Fase 3 — Índice y búsqueda
- [ ] Esquema Postgres en Supabase + `pgvector`.
- [ ] Chunking + embeddings + carga incremental por checksum.
- [ ] RPC `hybrid_search` con RRF.
- [ ] UI web mínima: caja de búsqueda, resultados con podcast/episodio/timestamp, enlace `#t=`.

### Fase 4 — Refinamiento
- [ ] Diarización (§5.7) y mapeo de hablantes.
- [ ] Filtros por facetas y taxonomía.
- [ ] Caché selectiva de audio.
- [ ] Automatización con `launchd`.

---

## 14. Decisiones pendientes (`TODO`)

1. **Modelo de embeddings definitivo** — condiciona la dimensión del esquema. Sugerido: `multilingual-e5-large` (1024).
2. **Proveedor de LLM para el enriquecimiento** — local vs. API. Afecta a coste y a si el enriquecimiento puede correr offline.
3. **¿UI web propia o cliente ligero sobre la API de Supabase?** Para consumo desde Android, una PWA sencilla puede bastar.
4. **Política de caché de audio**: nada / favoritos / todo.
5. **Configuración de `tsvector`**: `spanish`+`english` por episodio (sugerido) vs. `simple` uniforme.
6. **Confirmar identidad del podcast #3** frente al homónimo (§2.1).
7. **Tamaño de modelo whisper**: validar si `medium` es suficiente o hace falta `large-v3`.

---

## 15. Glosario del proyecto

- **Chunk**: fragmento de transcripción (~1–2 min) que constituye la unidad indexada y recuperable.
- **Búsqueda híbrida**: combinación de recuperación léxica (coincidencia exacta de términos) y semántica (similitud de significado vía embeddings).
- **RRF**: *Reciprocal Rank Fusion*, método de fusión de rankings basado en posiciones, no en scores.
- **Diarización**: identificación de qué hablante pronuncia cada fragmento.
- **Front matter**: bloque YAML de metadatos al inicio de un fichero Markdown.
- **Enclosure**: elemento del RSS que contiene la URL del fichero de audio del episodio.
