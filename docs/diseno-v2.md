---
proyecto: podcast-kb
titulo: "Base de conocimiento de transcripciones de podcasts de IA"
version: 2.0
fecha: 2026-08-21
idioma_doc: es
estado: especificación funcional y técnica (pre-código)
reemplaza: docs/diseno-v1.md
revision_aplicada: docs/revision-diseno-v1.md
---

# Proyecto: Base de conocimiento de transcripciones de podcasts

> **Propósito de este documento**: entrada única (contexto raíz) para un agente de código
> que vaya a implementar el sistema. Contiene objetivos, arquitectura, contratos de datos,
> esquemas y decisiones. Sustituye por completo a `diseno-v1.md`, que se conserva solo como
> registro histórico. Lo marcado como `TODO` requiere confirmación humana.

> **Cambios respecto a v1** (justificación completa en `revision-diseno-v1.md`, con la
> referencia entre paréntesis):
> corrección del flag `-ml 1` (A.0) · modelo por defecto `large-v3-turbo` + VAD (D.1, C.7) ·
> Python confirmado como disponible, `mlx-whisper` a evaluar (A.1) · RLS y repo privado
> (A.2) · publicidad dinámica (A.3) · reproductor propio en v1 (A.4) · índice léxico como
> columnas generadas (A.6) · denormalización para filtrado sobre HNSW (B.1) · `halfvec`
> (B.2) · doble checksum (B.3) · diversificación por episodio (B.5) · `podcast:transcript`
> (C.1) · dedupe global por enclosure (C.3) · máquina de estados con errores (C.4) ·
> diarización adelantada (D.4) · reordenación de fases (D.5).

---

## 1. Objetivo

Construir un sistema personal que:

1. Siga una lista **extensible** de podcasts (principalmente sobre IA, en español con
   anglicismos, y potencialmente en inglés).
2. Descargue automáticamente los episodios nuevos desde sus **feeds RSS públicos**.
3. Los transcriba a texto con **timestamps**, en local y sin coste por minuto.
4. Enriquezca cada transcripción con metadatos derivados (resumen, temas, entidades).
5. Los publique como **ficheros Markdown** (fuente de verdad, versionables).
6. Los indexe en un backend con **búsqueda híbrida** (léxica + semántica).
7. Permita buscar y **saltar al minuto exacto** del episodio relevante, desde escritorio o
   móvil.

### 1.1 No-objetivos (fuera de alcance v1)

- No se usa la API de Spotify: no expone transcripciones por API pública. Toda la ingesta
  parte del RSS original.
- No se re-publican las transcripciones públicamente (§12).
- No se hace transcripción en el propio Android.
- **No se construye un reproductor de podcasts.** Sí entra en v1 un `<audio>` mínimo en la
  UI de búsqueda que arranca en el segundo correcto — son ~20 líneas y sin ellas el
  resultado de búsqueda no es accionable en móvil (§9).

---

## 2. Fuentes iniciales

| # | Podcast | Autores | Idioma | Pistas de feed | Estado |
|---|---------|---------|--------|----------------|--------|
| 1 | Inteligencia Artificial Semanal | Gargoyles Devon (`gargoylesdevon@gmail.com`) | es | `pod.link/1723256857` · iVoox `f12364979` · YouTube `UC4ewDq9cCPd9eqA88M6yvEQ` · Spotify `48stHRcIdLoDi8WPK8JJWq` | Feed a resolver desde Apple ID `1723256857` |
| 2 | El Test de Turing — IA Aplicada a Negocio | Álvaro Peña, Arnau Vendrell, Víctor Mollá | es | `https://anchor.fm/s/e1671d44/podcast/rss` · Apple `id1771978939` · YouTube `@ElTestdeTuring` | Feed RSS directo identificado, pendiente de validar |
| 3 | La Tertul-IA: Inteligencia Artificial y más | Lu Martín, Frankie Carrero, Corti (AI Hackers / Product Hackers) | es | Apple `id1723736263` · Spotify `2trIE4WvCf3AWzNaLodxC4` · iVoox `f12478692` · newsletter `tertulia.mumbler.io` | Identidad **confirmada**; feed a resolver desde Apple ID |

### 2.1 Desambiguación (resuelta)

Existe un podcast distinto y homónimo: **"La TERTULia de la Inteligencia Artificial"**
(ironbar.github.io, Apple `id1669083682`). **No es el mismo** que el #3. Verificado por
autores: el #3 lo presentan Lu Martín, Frankie Carrero y Corti, del equipo de AI Hackers.

### 2.2 Aviso: el #3 se publica en dos feeds

Los episodios de La Tertul-IA aparecen **también** dentro de *Growth: el podcast de Product
Hackers* (Apple `id1236733939`, Spreaker show `4955285`), numerados como "La Tertul-IA #NN".

Es el mismo audio con `guid` distinto y bajo otro `podcast_id`, así que la clave
`UNIQUE (podcast_id, guid)` **no puede** detectarlo. Si algún día se da de alta el feed de
Growth, la deduplicación global por enclosure (§4.2) es lo único que evita transcribir dos
veces y devolver resultados duplicados.

### 2.3 Resolución de feed a partir de un ID de Apple Podcasts

```
GET https://itunes.apple.com/lookup?id=<APPLE_ID>&entity=podcast
→ results[0].feedUrl
```

Vía canónica para dar de alta un podcast del que solo se conoce su enlace de Apple o de
`pod.link` (mismo ID). Se implementa como `podcast-kb add --apple-id ...`.

Al validar cada feed, comprobar además si trae elementos de Podcasting 2.0 (§4.4):

```bash
curl -s "$FEED" | grep -oE '<podcast:(transcript|chapters)[^>]*>' | head
```

Notas por podcast:
- **#1**: iVoox suele exponer `https://www.ivoox.com/feed_fg_f<ID>_filtro_1.xml`. Contrastar
  con lo que devuelva Apple y quedarse con el canónico de Apple.
- **#2**: verificar que `anchor.fm/s/e1671d44/podcast/rss` responde 200 y no redirige a otro
  dominio; **persistir la URL final, no la inicial**.
- **#3**: si Apple devuelve el feed de Mumbler o iVoox, dar de alta ese, nunca el de Growth
  (§2.2).

---

## 3. Arquitectura general

Sistema **desacoplado en dos mitades** por restricciones de equipo y conectividad:

```
┌───────────────────────────────┐        ┌──────────────────────────────┐
│   MITAD LOCAL (MacBook Air)   │        │   MITAD REMOTA               │
│                               │        │                              │
│  1. Ingesta RSS               │        │  Git repo privado (Markdown) │
│  2. Descarga de audio         │  push  │        ↓                     │
│  3. Speech-to-text (whisper)  │ ─────► │  Indexado (GitHub Action     │
│  4. Diarización (opcional)    │   MD   │   o local): chunk + embed    │
│  5. Emisión de ficheros .md   │        │        ↓                     │
│                               │        │  Supabase / Postgres         │
│  Estado local: SQLite         │        │    · tsvector (FTS)          │
└───────────────────────────────┘        │    · pgvector (embeddings)   │
                                         │        ↓                     │
                                         │  Edge Function (auth)        │
                                         │        ↓                     │
                                         │  PWA de búsqueda             │
                                         └──────────────────────────────┘
                                                     ▲
                                                     │ consulta
                                                Android / navegador
```

**Principio rector**: el Mac hace solo lo pesado y produce **artefactos de texto plano**.
Todo lo demás (chunking, embeddings, índice, UI) ocurre aguas abajo y puede rehacerse desde
cero a partir de los `.md`, sin volver a transcribir nada.

### 3.1 Consecuencias del desacople

- Los `.md` son **idempotentes y reproducibles**: reindexar es barato, retranscribir es caro.
- El Mac puede estar offline durante horas; sincroniza cuando puede.
- La transcripción se puede migrar a otra máquina sin tocar el backend. En particular, el
  **backfill histórico puede hacerse una sola vez en una máquina con GPU** (~3–5 h en vez de
  ~20–35 h) y dejar al Air solo el incremental semanal (~20 min/semana).

### 3.2 El enriquecimiento y el embedding van aguas abajo

Corolario del principio rector que conviene fijar explícitamente: **los embeddings se
calculan en la etapa de indexado (`src/index/`), no en la de exportación**. Si se generasen
al exportar, el Mac pasaría a ser necesario para reindexar. Generándolos al indexar, basta
el repo — y eso permite ejecutar la indexación en un **GitHub Action disparado por `push`**,
con el Mac apagado (gratis en repo privado, 2.000 min/mes).

### 3.3 La única cosa que no se puede posponer: el audio

El diseño decide no conservar los MP3 (§9). Eso convierte en irreversible toda decisión que
dependa del audio:

- La **diarización** (§5.7) se aplica sobre el audio. Diarizar después del backfill obliga a
  redescargar 260 h — y con inserción dinámica de publicidad (§9.1) ese audio ya no es el
  mismo que se transcribió, así que los timestamps no cuadrarán.
- Lo mismo vale para cualquier reproceso acústico futuro.

**Regla**: decidir si se diariza **antes** del backfill masivo. Si se decide aplazarlo,
conservar el WAV normalizado (~100 MB/h; ~26 GB para todo el backfill, borrables después).
Es la única excepción al principio de "no guardar audio", y es mucho más barata que la
alternativa.

---

## 4. Etapa 1 — Ingesta RSS

### 4.1 Registro de podcasts

`config/podcasts.yaml` es la **fuente de verdad** del alta de podcasts: declarativo y
versionado en Git, coherente con el resto del diseño. SQLite es caché derivada + estado de
ejecución.

`podcast-kb add` escribe en el YAML y luego reconcilia; `sync` reconcilia al arrancar. Si
divergen, gana el YAML.

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

- `UNIQUE (podcast_id, guid)` en `episodes`. Un `INSERT OR IGNORE` deduplica entre
  ejecuciones **dentro de un mismo podcast**.
- **Además**, índice `UNIQUE` **global** sobre `enclosure_sha256` (hash de la URL del
  enclosure normalizada: sin query de tracking, sin prefijos de redirección tipo `podtrac`,
  `chartable`, `pdst.fm`). Es lo único que detecta el mismo audio publicado en dos feeds
  distintos (§2.2). Comprobarlo **antes** de descargar.
- Cabeceras condicionales `ETag` / `If-Modified-Since` al pedir el feed.
- Los reintentos son idempotentes: si un fichero de audio ya existe con el tamaño esperado,
  no se vuelve a descargar.

### 4.3 Progreso y errores

**No** una máquina de estados lineal: el enriquecimiento es re-ejecutable sobre episodios ya
indexados (§6), y una cadena lineal no lo modela. En su lugar, un campo `stage` con el
máximo alcanzado más **timestamps independientes por etapa**:

```
discovered → downloaded → transcribed → [diarized] → [enriched] → exported → indexed
```

Cada etapa persiste su `*_at`. Una ejecución interrumpida se retoma exactamente donde se
quedó, y una etapa concreta puede rehacerse sin tocar las demás.

**Reintentos con backoff**: `attempts`, `next_retry_at` y `last_error`. Sin esto, un episodio
cuyo audio da 404 se reintenta en cada ejecución para siempre — y con `process --pending N`
llega a copar el lote entero, impidiendo que avancen los que sí funcionan. La espera crece
15 min → 30 → 1 h → 2 h y se abandona tras 5 intentos, marcando el episodio para revisión.
Un episodio que termina bien borra su historial de fallos.

**Revisión de calidad**: flag `needs_review` (§5.8).

### 4.4 Comprobar Podcasting 2.0 antes de transcribir

Antes de descargar el audio, mirar si el ítem trae:

- **`<podcast:transcript>`** — transcripción ya publicada en SRT/VTT/JSON. Cuando existe,
  suele estar revisada y es mejor que la de Whisper. Descargarla, convertirla al formato de
  `.segments.json` y **saltarse la transcripción**, marcando `transcript.engine: "feed"`.
- **`<podcast:chapters>`** (JSON) o `<psc:chapters>` (formato antiguo) — capítulos con
  timestamps, exactamente el campo `chapters[]` del §6, sin coste de LLM.

Un feed puede declarar **varios formatos por episodio**. Hay que elegir por preferencia,
no por orden de aparición: `text/vtt` > SubRip > JSON > texto plano. El texto plano **no
sirve** para lo que hacemos —sin marcas de tiempo no hay salto al minuto (§5.12)—, así que
un episodio que solo lo traiga hay que transcribirlo igual.

Los subtítulos WebVTT pueden traer además el hablante en etiquetas `<v Nombre>`, y
`<podcast:person>` declara el reparto del episodio: entre los dos resuelven de gratis el
mapeo de §5.11.

Medido sobre el corpus real: 128 de 566 episodios (148 h) llegan ya transcritos con marcas
de tiempo. Comprobarlo cuesta un `grep` y ahorra un 22% del backfill.

### 4.5 Planificación

- Ejecución manual (`podcast-kb sync`) y/o `launchd` en macOS (preferible a `cron` en
  portátiles: respeta suspensiones y recupera ejecuciones perdidas).
- Cadencia diaria. Los tres podcasts son semanales.
- Envolver en `caffeinate -i` para que la suspensión no corte los lotes.
- `--max-episodes N` para acotar cada ejecución (§5.9).

---

## 5. Etapa 2 — Speech-to-text

### 5.1 Decisión: Whisper local, coste cero

El modelo Whisper es open source (MIT). Ejecutarlo en local no tiene coste por minuto, ni
límite, ni requiere cuenta. Es el camino por defecto de todo el sistema.

> No confundir con la **API alojada** de Whisper (OpenAI, ~0,36 USD/h), que sirve el mismo
> modelo desde servidores de terceros. No se usa: si un episodio falla la validación de
> §5.10, la respuesta es `large-v3` completo en local — gratis y normalmente mejor, porque
> la API sirve `large-v2` en la mayoría de los casos.

### 5.2 Entorno objetivo: MacBook Air (Apple Silicon)

Restricción real del equipo: **no se puede instalar software comercial**. Python sí está
disponible sin problema. Todo lo que se especifica aquí es open source y gratuito (§13).

**Implementación por defecto: `whisper.cpp`**, por:

1. **Aceleración Metal**, que aprovecha la GPU integrada del Apple Silicon.
2. **VAD integrado**, que es la palanca principal contra las alucinaciones (§5.8).
3. Cuantización (`q5_0`, `q8_0`) para ajustar RAM sin recompilar.
4. Madurez y estabilidad de la CLI.

**Alternativa a medir en la Fase 1: `mlx-whisper`.** Implementación sobre MLX, el framework
de Apple para Apple Silicon (`pip install mlx-whisper`, MIT). En M-series compite de tú a tú
con whisper.cpp y a menudo lo supera con modelos grandes. Medirlo cuesta 20 minutos y la
diferencia se multiplica por las ~260 h del backfill. Contrapartida: no trae VAD integrado
(habría que añadir `silero-vad` aparte).

**`faster-whisper` NO es recomendable en este equipo.** CTranslate2 no tiene backend Metal,
así que en Mac corre solo en CPU: se renuncia a la GPU y se multiplican los tiempos. Es
excelente en máquinas con CUDA — como la que se use para el backfill (§3.1) — pero no aquí.

### 5.3 Modelo por defecto: `large-v3-turbo`

`ggml-large-v3-turbo-q5_0.bin` (~570 MB). 809M parámetros con el decoder reducido de 32 a 4
capas: del orden de 5–8× más rápido que `large-v3` y de calidad claramente superior a
`medium`, que era el default de la v1.

Escalón superior para episodios que fallen la validación (§5.10): `large-v3` completo.

**Validación obligatoria en Fase 1**: la degradación conocida de `turbo` se concentra en
audio ruidoso y con **habla solapada** — que es exactamente lo que pasa en una tertulia a
tres voces, dos de los tres podcasts. Comparar `turbo` contra `large-v3` **en un tramo con
gente hablando encima**, no en un monólogo limpio. Es una prueba de 20 minutos que decide
horas de cómputo.

### 5.4 Preprocesado de audio

```bash
ffmpeg -i input.mp3 -ar 16000 -ac 1 -c:a pcm_s16le output.wav
```

(16 kHz, mono, PCM 16-bit: formato nativo de whisper.cpp.)

Registrar la duración real del WAV en `transcribed_duration_sec` — no es lo mismo que la
`duration_sec` que declara el feed, y la diferencia importa (§9.1).

### 5.5 Idioma

**No fijar un idioma global.** Es un campo por podcast (`podcasts.language`), con override
por episodio, y se pasa en cada invocación.

Motivo: el corpus mezclará podcasts en español y en inglés. Un `--language` global degradaría
unos u otros. La autodetección (`-l auto`) se evita porque en español-con-muchos-anglicismos
puede oscilar dentro del mismo episodio.

### 5.6 Invocación

```bash
whisper-cli \
  -m models/ggml-large-v3-turbo-q5_0.bin \
  -f ep.wav \
  -l es \
  --prompt "$(cat config/glossary.es.txt)" \
  --vad --vad-model models/ggml-silero-v5.1.2.bin \
  -oj -of transcripts/<slug>/<fichero>
```

**`-ml 1` NO debe usarse** (estaba en la v1 por error). `-ml`/`--max-len` es longitud máxima
de segmento **en caracteres**, no en segundos: `-ml 1` parte en cada token y produce
timestamps a nivel de token, no los segmentos de 3–8 s que asume §7.2.

Si en el futuro se quieren timestamps por palabra —que para el salto temporal es un lujo
real— la forma correcta es deliberada: `-ml 1 -sow` (*split on word*), decidiéndolo en §7.2
y asumiendo que el `.segments.json` crece de 5 a 10×. `TODO`: decidir en Fase 1.

### 5.7 Glosario de anglicismos

Whisper acepta un prompt inicial que condiciona el vocabulario. Se mantiene en
`config/glossary.<lang>.txt`, editable, con un glosario opcional por podcast que se
concatena al global.

**Redactarlo como prosa, no como lista.** El prompt es contexto previo simulado: si el
"contexto previo" es una lista de términos separados por comas, el modelo tiende a
**continuar la lista** y a escupirla al principio de la transcripción. Forma correcta:

```
En este episodio hablamos de LLMs, embeddings, fine-tuning, RAG, agentes y MCP,
con context window y quantization, sobre modelos de OpenAI, Anthropic, Google,
Meta, Mistral y DeepSeek, y herramientas como Hugging Face, Cursor y Copilot.
```

Notas:
- Límite de ~224 tokens. Si crece, priorizar por frecuencia o rotar por podcast.
- **El efecto decae**: condiciona sobre todo las primeras ventanas de 30 s, no el minuto 40.
  La palanca de consistencia a lo largo del episodio es `config/fixups.tsv`, no el prompt.
- Descartar en post-proceso el primer segmento si su similitud con el prompt supera un
  umbral.

### 5.8 Alucinaciones — el fallo de calidad más probable

Los dos modos de fallo de Whisper en podcasts:

- **Alucinación en silencio/música**: sintonías, cortinillas y colas producen texto
  inventado. En español el clásico es *"Subtítulos realizados por la comunidad de
  Amara.org"* y variantes de despedida, aprendidas de subtítulos de YouTube.
- **Bucles de repetición**: una frase repetida decenas de veces cuando el audio se degrada.

Mitigaciones, por orden de efectividad:

1. **VAD** (`--vad`, §5.6). Recorta silencios antes de decodificar y elimina la mayoría de
   las alucinaciones de raíz. Es la palanca grande.
2. Frases fantasma conocidas en `config/fixups.tsv`.
3. **Detector de n-gramas repetidos** en post-proceso, que marca `needs_review = 1` en vez
   de publicar el episodio en silencio. Sin esto no hay forma de enterarse de que un
   episodio salió mal salvo leyéndolo entero.

### 5.9 Presupuesto de tiempo y térmico

El Air es *fanless*: en lotes largos hay throttling. Órdenes de magnitud en un M2/M3 con
Metal:

| Modelo | Factor tiempo real | 1 h de audio | Backfill (~306 h medidas, 2 de 3 feeds) |
|---|---|---|---|
| `medium-q5_0` | ~5–8× | 8–12 min | 38–61 h |
| `large-v3-turbo-q5_0` | ~8–15× | 4–8 min | 20–38 h |
| `large-v3-q5_0` | ~2–3× | 20–30 min | 102–153 h |

Volumen real medido sobre los cuatro feeds: **566 episodios, 664 h** de audio.

| Podcast | Episodios | Horas | Ya transcritos por el feed | Con capítulos |
|---|---:|---:|---:|---:|
| IA Semanal | 95 | 68 | — | — |
| El Test de Turing | 166 | 239 | — | 58 |
| La Tertul-IA | 127 | 141 | — | 18 |
| monos estocásticos | 178 | 216 | **128 (148 h)** | 59 |
| **Total** | **566** | **664** | **128 (148 h)** | **135** |

Aprovechar las transcripciones que ya publica Cuonda (§4.4) quita **148 h** del
backfill: quedan 516 h, es decir **34–64 h de máquina** con `large-v3-turbo` en vez
de 44–83 h. Es la mayor optimización disponible y no cuesta nada.

Restar un 20–30% en lotes largos por throttling sostenido. Prácticas:

- Lotes nocturnos de `--max-episodes 10`, portátil sobre superficie dura, `caffeinate -i`.
- Considerar hacer el **backfill una sola vez** en una máquina con GPU (§3.1).
- El incremental semanal son ~3 episodios, ~20 min de máquina: trivial en el Air.

### 5.10 Criterio de aceptación (Fase 1)

Sin criterio explícito, la validación se marca por cansancio. Protocolo:

Tomar 3 tramos de 3 minutos, uno por podcast, elegidos **en zonas de conversación cruzada**.
Contar (a) errores en términos del glosario y (b) frases incomprensibles o alucinadas.

**Umbral de paso**: <2 errores de glosario por tramo y 0 alucinaciones.

Si no pasa, el orden de intervención es: subir de `turbo` a `large-v3` → revisar VAD →
ampliar glosario → añadir fixups. Es el orden de mayor efecto por unidad de esfuerzo.

### 5.11 Diarización (quién habla)

Dos de los tres podcasts son tertulias a 3 voces. Separar hablantes mejora notablemente la
búsqueda ("qué dijo Corti sobre X").

Whisper no diariza; requiere una pasada adicional con `pyannote.audio` (open source, MIT;
requiere cuenta en Hugging Face y aceptar la licencia gratuita del modelo
`speaker-diarization-3.1`). Corre sobre torch/MPS en Apple Silicon.

**Decidir antes del backfill, no después** — ver §3.3, que es la razón. No es una
preferencia estética: es la única decisión del proyecto que se encarece al posponerla.

**Mapeo de hablantes**: el problema real es que el orden de `SPEAKER_00`, `SPEAKER_01`… no
es estable entre episodios, así que un mapa por episodio significa etiquetar 350 veces a
mano. Solución: extraer **una vez** 15–20 s de voz limpia de cada tertuliano, calcular su
embedding de locutor, y asignar cada cluster al tertuliano más cercano por similitud de
coseno. Se etiqueta una vez por podcast (`config/speakers.<slug>.yaml`) y sobrevive a los
cambios de orden. Un invitado que no se parezca a nadie se queda como `SPEAKER_XX`, que es
el comportamiento deseable.

### 5.12 Salida requerida

JSON con segmentos y timestamps (`-oj`), no texto plano. Cada segmento: `{start, end, text}`
más `speaker` si hay diarización. Los timestamps son la base de todo el valor del sistema.

---

## 6. Etapa 3 — Enriquecimiento

Por cada transcripción completa, generar con un LLM:

| Campo | Descripción |
|-------|-------------|
| `summary` | Resumen de 3–5 frases. |
| `topics[]` | 5–10 temas de una taxonomía controlada (`config/topics.yaml`). |
| `entities[]` | Modelos, empresas, personas, productos mencionados. |
| `keywords[]` | Términos para el índice léxico. |
| `chapters[]` | Secciones con `start`. **Preferir siempre los del autor** si el feed los trae (§4.4) o si están en las show notes; marcar el origen en `chapters_source`. |

Notas:

- La **taxonomía controlada** de `config/topics.yaml` es lo que hace útil el filtrado por
  facetas. Pedir al modelo que se ciña a ella, con un cajón `otros` que se revisa
  periódicamente. Sin esto los temas divergen y las facetas dejan de servir.
- Para episodios largos, resumir por chunks y consolidar (map-reduce).
- Etapa **opcional para la v1** y re-ejecutable sobre los `.md` existentes. El sistema es
  útil sin ella.
- Aprovechar `<description>` / `<itunes:summary>` del RSS como metadato de origen, sin coste.
- **Proveedor**: por defecto, LLM local (`Ollama` / `llama.cpp`, 7–14B), suficiente para
  resumir y extraer entidades sobre texto ya transcrito. Ver §13: es la única decisión del
  proyecto con coste recurrente potencial, y es reversible (se reprocesa desde los `.md` sin
  retranscribir).

---

## 7. Contratos de datos

### 7.1 Estado local (SQLite, en el Mac)

```sql
CREATE TABLE podcasts (
  id              INTEGER PRIMARY KEY,
  slug            TEXT NOT NULL UNIQUE,     -- 'test-de-turing'
  title           TEXT NOT NULL,
  authors         TEXT,
  rss_url         TEXT NOT NULL UNIQUE,     -- URL final tras redirecciones
  apple_id        TEXT,
  spotify_show_id TEXT,
  website         TEXT,
  language        TEXT NOT NULL,            -- 'es' | 'en'
  glossary_path   TEXT,                     -- glosario específico opcional
  speaker_map     TEXT,                     -- config/speakers.<slug>.yaml
  active          INTEGER NOT NULL DEFAULT 1,
  etag            TEXT,                     -- caché condicional del feed
  last_modified   TEXT,
  last_synced_at  TEXT
);

CREATE TABLE episodes (
  id             INTEGER PRIMARY KEY,
  podcast_id     INTEGER NOT NULL REFERENCES podcasts(id),
  guid           TEXT NOT NULL,
  enclosure_sha256 TEXT NOT NULL,           -- §4.2, dedupe GLOBAL
  title          TEXT NOT NULL,
  episode_number INTEGER,
  published_at   TEXT NOT NULL,             -- ISO 8601
  duration_sec   INTEGER,                   -- la que declara el feed
  transcribed_duration_sec INTEGER,         -- la del WAV realmente transcrito (§9.1)
  audio_url      TEXT NOT NULL,             -- URL final tras redirecciones
  audio_bytes    INTEGER,
  description    TEXT,
  episode_url    TEXT,
  language       TEXT,                      -- hereda de podcasts, override posible
  local_audio    TEXT,
  md_path        TEXT,
  transcript_engine TEXT,                   -- 'whisper.cpp' | 'mlx-whisper' | 'feed'
  transcript_model  TEXT,                   -- 'ggml-large-v3-turbo-q5_0'
  checksum_audio TEXT,                      -- de la copia transcrita (§9.1)

  -- progreso: stage + timestamps independientes (§4.3)
  stage          TEXT NOT NULL DEFAULT 'discovered',
  discovered_at  TEXT NOT NULL,
  downloaded_at  TEXT,
  transcribed_at TEXT,
  diarized_at    TEXT,
  enriched_at    TEXT,
  exported_at    TEXT,
  indexed_at     TEXT,

  -- errores y calidad
  attempts       INTEGER NOT NULL DEFAULT 0,
  next_retry_at  TEXT,
  last_error     TEXT,
  needs_review   INTEGER NOT NULL DEFAULT 0,

  UNIQUE (podcast_id, guid)
);

-- Detecta el mismo audio publicado en dos feeds distintos (§2.2)
CREATE UNIQUE INDEX episodes_enclosure_idx ON episodes (enclosure_sha256);

-- Caché de embeddings por hash de texto, para no re-embeder al reajustar chunking
CREATE TABLE embeddings_cache (
  text_sha256 TEXT PRIMARY KEY,
  model       TEXT NOT NULL,
  embedding   BLOB NOT NULL
);
```

### 7.2 Artefacto de salida: fichero Markdown con front matter

**Un fichero por episodio.** Es la fuente de verdad y el contrato entre las dos mitades.

Ruta: `transcripts/<podcast_slug>/<YYYY-MM-DD>-<episode-slug>.md`

```markdown
---
schema_version: 2
podcast: "El Test de Turing - IA Aplicada a Negocio"
podcast_slug: test-de-turing
authors: ["Álvaro Peña", "Arnau Vendrell", "Víctor Mollá"]
episode_title: "Agentes IA: Destripando los enigmas con un PRO"
episode_number: 121
guid: "e1671d44-xxxx-xxxx"
published_at: "2026-07-15T06:00:00Z"
duration_sec: 3782                 # declarada por el feed
transcribed_duration_sec: 3821     # medida sobre el audio transcrito (§9.1)
language: es
audio_url: "https://.../episode.mp3"
episode_url: "https://open.spotify.com/episode/17erE3notlcJ3pyFXqY3YW"
transcript:
  engine: "whisper.cpp"            # 'feed' si vino de <podcast:transcript>
  model: "ggml-large-v3-turbo-q5_0"
  transcribed_at: "2026-08-21T10:22:00Z"
  glossary: "es-tech-v1"
  vad: true
  diarized: false
  needs_review: false
enrichment:
  summary: "..."
  topics: ["agentes", "herramientas-dev", "frameworks"]
  entities: ["Claude Code", "CodeGPT", "Daniel Ávila"]
  keywords: ["agente", "terminal", "in-house"]
chapters_source: author            # author | llm | none
chapters:
  - { start: 0,    title: "Intro y presentación" }
  - { start: 62,   title: "Inteligencia artificial y SEO" }
source: rss
checksum_audio: "sha256:..."       # de la copia transcrita, NO detector de ediciones
---

# Agentes IA: Destripando los enigmas con un PRO (Ep. 121)

## Transcripción

[00:00:00] Texto del primer segmento agrupado...

[00:01:12] Siguiente bloque...

[00:02:45] (Frankie) Con diarización, el hablante va así.
```

**Reglas del formato:**

- Front matter **YAML**, por legibilidad y compatibilidad con Obsidian.
- Los segmentos crudos (3–8 s) se **agrupan en bloques de ~30–60 s** para legibilidad. El
  timestamp del bloque es el `start` del primer segmento.
- **Conservar los segmentos crudos** en `transcripts/<slug>/<fichero>.segments.json.gz`,
  para poder rehacer el chunking sin retranscribir. El `.md` es para humanos e indexado; el
  JSON es materia prima. **Comprimido**: 1 h de audio son 150–300 KB de JSON (0,5–1 MB con
  timestamps por palabra), y para 350 episodios eso son cientos de MB en Git que comprimen
  mal en delta. Gzip da ~5:1. El `.md` va sin comprimir, que es lo que da sentido a
  versionarlo (§10).
- Timestamps `[HH:MM:SS]`, siempre absolutos desde el inicio del audio.
- `schema_version` explícito, para migrar el formato en el futuro.
- Nombres de fichero ASCII, minúsculas, sin espacios, para evitar problemas entre
  macOS/Git/Linux.
- **Los checksums de reindexado NO van en el fichero** — se calculan al indexar y viven solo
  en Postgres (§8.5). Meter en el front matter un hash del propio front matter sería
  autorreferencial.

### 7.3 Backend (Supabase / Postgres)

```sql
create extension if not exists vector;

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
  transcribed_duration_sec int,
  audio_url text not null,
  episode_url text,
  language text not null,
  summary text,
  topics text[],
  entities text[],
  keywords text[],
  chapters jsonb,
  chapters_source text,
  needs_review boolean not null default false,

  -- reindexado incremental (§8.5)
  transcript_checksum text,   -- hash del CUERPO: decide re-chunk + re-embed
  meta_checksum text,         -- hash del FRONT MATTER: decide un UPDATE barato
  indexed_at timestamptz,

  unique (podcast_id, guid)
);

create table chunks (
  id bigserial primary key,
  episode_id uuid not null references episodes(id) on delete cascade,
  idx int not null,
  start_sec int not null,
  end_sec int not null,
  speaker text,                          -- reservado para diarización (§5.11)
  content text not null,

  -- denormalizado para poder filtrar ANTES del ranking vectorial (§8.4)
  podcast_id uuid not null,
  published_at timestamptz not null,
  language text not null,

  tsv_es tsvector generated always as (to_tsvector('spanish', content)) stored,
  tsv_en tsvector generated always as (to_tsvector('english', content)) stored,
  embedding halfvec(1024),               -- §8.2

  unique (episode_id, idx)
);

-- NO parciales a propósito: ver §7.4.
create index chunks_tsv_es_idx on chunks using gin (tsv_es);
create index chunks_tsv_en_idx on chunks using gin (tsv_en);

create index chunks_embedding_idx on chunks
  using hnsw (embedding halfvec_cosine_ops);

create index chunks_podcast_idx   on chunks (podcast_id, published_at desc);
create index episodes_published_idx on episodes (published_at desc);
create index episodes_topics_idx    on episodes using gin (topics);
```

### 7.4 Por qué dos columnas generadas, y por qué NO `unaccent`

**Acentos: no hace falta hacer nada.** El stemmer Snowball para español elimina los acentos
agudos en su fase final, así que `sesion` ya encuentra `sesión`, `codigo` encuentra `código`
y `Alvaro` encuentra `Álvaro` con la configuración `spanish` a secas. Medido contra
Postgres 16.

Añadir `unaccent` encima solo arregla un residuo marginal de stemming asimétrico
(`María`/`Maria`, `además`/`ademas`) y **a cambio colapsa la ñ**: `año`≡`ano`,
`campaña`≡`campana`, `cañón`≡`canon`, `sueño`≡`sueno`. La ñ es una letra, no una tilde; en un
corpus en español el intercambio no compensa. Un fichero de reglas `unaccent` a medida que
preservase la ñ lo resolvería, pero requiere escribir en `$SHAREDIR/tsearch_data/`, que en
Supabase gestionado no es posible.

**Dos columnas generadas en vez de una poblada por la aplicación.** Lo natural sería una sola
columna `tsv` con la configuración del idioma del episodio, pero **no se puede**:
`to_tsvector(regconfig, text)` no es `IMMUTABLE` con configuración **dinámica**, así que no
vale en una columna generada. Con un nombre de configuración **literal** sí lo es, que es
justo por lo que las dos columnas de arriba funcionan.

Poblar una columna `tsv` desde la aplicación también funciona, pero una columna que se olvida
de poblar es un índice que devuelve cero resultados sin dar error. Dos columnas generadas más
índices parciales cuestan algo de espacio —un `tsvector` vacío en la columna que no aplica es
barato— y eliminan esa clase de bug entera: no hay forma de insertar un chunk sin índice
léxico.

**Los índices léxicos NO son parciales, y la consulta no elige la columna con un
`CASE`.** Ambas cosas parecían buena idea y ambas rompen el índice. Medido sobre
20.000 chunks:

| Consulta | Plan |
|---|---|
| Columna literal, valor de idioma literal | `Bitmap Index Scan` ✅ |
| `case when lang='en' then tsv_en else tsv_es end`, idioma como parámetro | `Parallel Seq Scan` ❌ |
| Una rama por idioma, índices no parciales, idioma como parámetro | `Bitmap Index Scan` ✅ |

El motivo: con el idioma como parámetro y un **plan genérico** —al que Postgres
cambia tras varias ejecuciones de la misma sentencia— no puede saber qué columna
lleva el `CASE` ni demostrar el predicado `where language = 'es'` del índice
parcial, así que no puede usar ninguno de los dos. El RPC de §8.4 tiene por eso
una rama `lex_es` y otra `lex_en`, cada una con su columna fija; el planificador
resuelve la rama del idioma no usado con un `One-Time Filter` y no la ejecuta.

Un `tsvector` vacío aporta pocas claves a GIN, así que indexar también las filas
del otro idioma sale a un coste comparable al del índice parcial y elimina la
dependencia del plan.

**Consultar con `websearch_to_tsquery`**, no con `plainto_tsquery`: acepta comillas y
`-exclusión` sin sintaxis especial, que es lo que un humano teclea sin pensar.

### 7.5 Seguridad: RLS obligatorio

El §12 dice que las transcripciones no se publican. Una tabla Supabase **sin RLS es legible
por cualquiera con la `anon key`**, y la `anon key` va incrustada en el cliente web: es
pública por diseño. Sin esto, el esquema anterior publica el corpus entero en internet.

```sql
alter table podcasts enable row level security;
alter table episodes enable row level security;
alter table chunks   enable row level security;
-- sin policies: deny-all para anon.
```

La PWA **no habla con PostgREST directamente**. Habla con una Edge Function que valida
sesión y llama al RPC con `service_role` del lado servidor. Es una línea más de arquitectura
y evita el único fallo del proyecto con consecuencias fuera del portátil.

**El repositorio Git es privado.** Explícitamente.

---

## 8. Etapa 4 — Indexación híbrida

### 8.1 Chunking

- Unidad de indexado: **~1–2 minutos de audio** (~150–300 palabras en español hablado).
- **Solape del 15–20%** entre fragmentos consecutivos.
- Respetar fronteras de segmento de whisper: nunca cortar a mitad de frase.
- Si hay capítulos, alinear preferentemente las fronteras de chunk con las de capítulo.
- Cada chunk conserva `start_sec` / `end_sec`: es lo que permite el salto temporal (§9).
- El solape duplica hits en la rama léxica: se deduplica por solape temporal tras la fusión
  (§8.3).

### 8.2 Embeddings

**El modelo debe ser multilingüe.** Con corpus mixto es/en, un modelo monolingüe hace que una
consulta en español no recupere nada de los episodios en inglés.

| Modelo | Dim. | Notas |
|--------|------|-------|
| `multilingual-e5-large` | 1024 | MIT, local, muy sólido en es/en. Requiere prefijos `query:` / `passage:`. |
| `bge-m3` | 1024 | Apache, multilingüe, soporta también recuperación léxica. |

**Recomendación por defecto: `multilingual-e5-large`** vía `sentence-transformers`. Ambos
son 1024 dims, así que **el esquema no depende de cuál se elija** y la decisión puede
aplazarse a la Fase 3. Cambiar de modelo implica reembedding completo, que es barato: se
rehace desde los `.md`.

**Almacenamiento: `halfvec(1024)`** (pgvector ≥ 0.7). Reduce vectores e índice a la mitad
con pérdida de recall despreciable a esta escala. Justificación en §8.6.

**Caché por hash de texto** (`embeddings_cache`, §7.1): al reajustar tamaño de chunk o
solape, la mayoría del texto sigue siendo idéntico. Convierte un reajuste de parámetros en
una operación gratis.

**No olvidar los prefijos de e5**: `passage: ` al indexar, `query: ` al consultar. Usar uno
sin el otro degrada el recall de forma silenciosa.

### 8.3 Fusión de resultados: RRF

Ejecutar ambas búsquedas en paralelo y fusionar con **Reciprocal Rank Fusion**:

```
score(d) = Σ_i  1 / (k + rank_i(d))        con k ≈ 60
```

RRF es preferible a la suma ponderada porque no requiere normalizar escalas heterogéneas
(`ts_rank` vs. distancia coseno) ni calibrar pesos.

Tres detalles que deciden si la búsqueda se siente bien:

1. **Acotar cada rama a 50–100** antes de fusionar. Sin tope, una consulta con un término
   frecuente devuelve miles de filas por la rama léxica y la fusión se vuelve lenta y peor.
2. **Diversificar por episodio.** Sin esto, "agentes" devuelve los 10 chunks solapados del
   mismo bloque de 15 minutos del mismo episodio. Máximo 2–3 chunks por episodio, y presentar
   el resultado como *episodio + momentos relevantes dentro de él* — que además es lo que el
   usuario busca: un sitio al que saltar.
3. **Deduplicar por solape temporal**: dos chunks solapados sobre el mismo momento son el
   mismo resultado dos veces.

### 8.4 RPC `hybrid_search`

**El embedding de la consulta se calcula fuera** (en la Edge Function): Postgres no ejecuta
el modelo. La firma lo refleja.

```sql
create or replace function hybrid_search(
  q             text,
  q_embedding   halfvec(1024) default null,
  lang          text    default 'es',
  podcast_ids   uuid[]  default null,
  date_from     timestamptz default null,
  date_to       timestamptz default null,
  topics_filter text[]  default null,
  speaker_filter text   default null,
  k             int default 60,
  arm_limit     int default 60,
  max_per_episode int default 3,
  result_limit  int default 20
)
returns table (
  episode_id uuid, chunk_id bigint, start_sec int, end_sec int,
  speaker text, content text, score real
)
language sql stable
set hnsw.iterative_scan = 'relaxed_order'
as $$
with lex_es as (
  select c.id, ts_rank_cd(c.tsv_es, websearch_to_tsquery('spanish', q)) as rank
  from chunks c
  where lang = 'es'
    -- El idioma de la FILA, además del de la consulta: la configuración
    -- `english` tokeniza texto español sin quejarse, así que sin esto una
    -- búsqueda en inglés devolvería chunks en español mal analizados.
    and c.language = 'es'
    and c.tsv_es @@ websearch_to_tsquery('spanish', q)
    and (podcast_ids    is null or c.podcast_id = any(podcast_ids))
    and (date_from      is null or c.published_at >= date_from)
    and (date_to        is null or c.published_at <= date_to)
    and (speaker_filter is null or c.speaker = speaker_filter)
    and (topics_filter  is null or exists (
           select 1 from episodes e
           where e.id = c.episode_id and e.topics && topics_filter))
  order by rank desc
  limit arm_limit
),
lex_en as (
  select c.id, ts_rank_cd(c.tsv_en, websearch_to_tsquery('english', q)) as rank
  from chunks c
  where lang = 'en'
    -- El idioma de la FILA, además del de la consulta: la configuración
    -- `english` tokeniza texto español sin quejarse, así que sin esto una
    -- búsqueda en inglés devolvería chunks en español mal analizados.
    and c.language = 'en'
    and c.tsv_en @@ websearch_to_tsquery('english', q)
    and (podcast_ids    is null or c.podcast_id = any(podcast_ids))
    and (date_from      is null or c.published_at >= date_from)
    and (date_to        is null or c.published_at <= date_to)
    and (speaker_filter is null or c.speaker = speaker_filter)
    and (topics_filter  is null or exists (
           select 1 from episodes e
           where e.id = c.episode_id and e.topics && topics_filter))
  order by rank desc
  limit arm_limit
),
lex as (
  -- Una rama por idioma, cada una con su columna FIJA. Elegirla con un CASE
  -- impide que el planificador use el índice GIN en cuanto el idioma es un
  -- parámetro: ver §7.4, está medido.
  select id, row_number() over (order by rank desc) as rnk
  from (select * from lex_es union all select * from lex_en) t
),
vec as (
  -- Sin embedding de consulta, esta rama queda vacía y la fusión degrada a
  -- búsqueda léxica pura. Es lo que permite que la UI funcione antes de
  -- decidir dónde se calculan los embeddings (§8.2).
  select id, row_number() over (order by dist) as rnk
  from (
    select c.id, c.embedding <=> q_embedding as dist
    from chunks c
    where q_embedding is not null
      and c.language = lang
      and c.embedding is not null
      and (podcast_ids    is null or c.podcast_id = any(podcast_ids))
      and (date_from      is null or c.published_at >= date_from)
      and (date_to        is null or c.published_at <= date_to)
      and (speaker_filter is null or c.speaker = speaker_filter)
      and (topics_filter  is null or exists (
             select 1 from episodes e
             where e.id = c.episode_id and e.topics && topics_filter))
    order by c.embedding <=> q_embedding
    limit arm_limit
  ) t
),
fused as (
  select coalesce(l.id, v.id) as cid,
         coalesce(1.0 / (k + l.rnk), 0) + coalesce(1.0 / (k + v.rnk), 0) as score
  from lex l full outer join vec v on l.id = v.id
),
ranked as (
  select c.episode_id, c.id as chunk_id, c.start_sec, c.end_sec,
         c.speaker, c.content, f.score::real as score,
         row_number() over (partition by c.episode_id order by f.score desc) as rn
  from fused f join chunks c on c.id = f.cid
)
select episode_id, chunk_id, start_sec, end_sec, speaker, content, score
from ranked
where rn <= max_per_episode
order by score desc
limit result_limit;
$$;
```

**El embedding de la consulta es opcional.** Si no se pasa, la rama vectorial
queda vacía y RRF degrada a búsqueda léxica pura. Sin esto, la UI no podría
funcionar hasta tener resuelto dónde se calculan los embeddings, y ese es
justamente el TODO que el diseño deja abierto hasta la Fase 3.

**Por qué los filtros se repiten en las dos ramas en vez de factorizarse en una CTE común.**
Una CTE referenciada más de una vez **no se inlinea** en Postgres: se materializa. Si los
filtros vivieran en un `with filtered as (...)` compartido, la rama vectorial recibiría una
tabla materializada y el planificador **no podría usar el índice HNSW** para el
`order by ... <=> ...`, degradando la consulta a un escaneo secuencial sobre todo el corpus.
La duplicación es fea pero es lo que mantiene ambos índices en juego.

`hnsw.iterative_scan` (pgvector ≥ 0.8) es lo que hace que el filtrado no arruine el recall:
sin él, HNSW devuelve sus `arm_limit` vecinos globales y los filtros se comen la mayoría
*después*, dejando la consulta casi vacía. Con escaneo iterativo, el índice sigue buscando
hasta reunir suficientes candidatos que pasen el filtro.

### 8.5 Reindexado incremental: dos checksums, no uno

El `.md` mezcla dos cosas con ciclos de vida distintos: el **front matter**, que cambia cada
vez que se re-ejecuta el enriquecimiento (§6, explícitamente re-ejecutable), y el **cuerpo**,
que solo cambia si se retranscribe o se aplican fixups.

Con un checksum único del fichero entero, re-enriquecer 350 episodios para probar un prompt
nuevo invalidaría ~17.500 chunks y dispararía un re-embedding completo innecesario.

- `transcript_checksum` — hash del cuerpo tras el `---` de cierre → re-chunk + re-embed.
- `meta_checksum` — hash del front matter → `UPDATE` barato sobre `episodes`.

`--force` para reindexado completo.

**Huérfanos.** Borrar un `.md` no quitaba su episodio del índice: seguía apareciendo en las
búsquedas apuntando a algo que ya no existe, y renombrar el slug de un podcast dejaba el
viejo con todos sus episodios dentro. El indexado los detecta comparando lo que hay en la
base con lo que hay en el repositorio, los reporta siempre y los borra con `--prune`. La
comprobación se salta si no se ha leído ningún `.md`: un `--root` equivocado vaciaría el
índice entero.

### 8.6 Presupuesto de almacenamiento

Con ~50 chunks por hora de audio:

| Concepto | Por episodio (~45 min) | 350 episodios |
|---|---|---|
| Vectores `halfvec(1024)` (2 B/dim) | ~75 KB | ~26 MB |
| Índice HNSW (m=16) | ~55 KB | ~19 MB |
| Texto de chunks + `tsvector` ×2 | ~90 KB | ~31 MB |
| **Total aproximado** | **~220 KB** | **~76 MB** |

Con `vector(1024)` en vez de `halfvec` serían ~120 MB: cabe en los 500 MB del free tier de
Supabase, pero con poco margen una vez sumados WAL, bloat y un reindexado en caliente.
`halfvec` da aire suficiente para no tener que pensarlo.

**Advertencia operativa**: un proyecto Supabase en free tier **se pausa** tras un periodo de
inactividad. Para un sistema personal consultado a ráfagas eso significa esperas al
despertar. Mitigación gratuita: un `ping` semanal desde el `launchd` que ya existe (§4.5).

---

## 9. Acceso al audio y salto temporal

**Decisión: no duplicar los MP3 por defecto.** Se persiste `audio_url` y se salta al segundo
exacto.

**El salto se hace en la propia PWA**, no delegando en el sistema operativo:

```js
audio.src = audio_url;
audio.currentTime = Math.max(0, start_sec - 10);   // margen por §9.1
```

**Requisito del hosting: peticiones `Range`.** Saltar dentro de un audio exige que el
servidor responda `206 Partial Content` a las peticiones con cabecera `Range`. Sin eso el
navegador solo puede reproducir desde el principio: `audio.currentTime = 932` se ignora
silenciosamente y vuelve a 0. Comprobado en el navegador. Los CDN de podcasts lo soportan,
pero conviene verificarlo por podcast al dar de alta el feed, y **es un requisito para la
caché de audio de §9.1**: lo que sirva las copias cacheadas tiene que soportar `Range`
también.

**Detalle de implementación:** el `<audio>` nace con `preload="none"` para no descargar
decenas de MB al abrir la página. Pero entonces asignar `src` no dispara ninguna carga, así
que `loadedmetadata` nunca llega y el salto no ocurre. Hay que llamar a `load()`
explícitamente. Es un fallo que no se ve leyendo el código: el usuario pulsa un resultado y
no pasa nada.

**Lo que se muestra no es el chunk entero.** Un chunk son ~90 s, unas 200 palabras: la
unidad correcta para indexar y para saltar, y un muro de texto en una pantalla de móvil. La
UI recorta un extracto de ~200 caracteres centrado en la primera coincidencia, con un enlace
para desplegar el fragmento completo.

Por qué no basta `#t=<segundos>`, que era la apuesta de la v1: un enlace a un `.mp3` desde
Chrome en Android abre el gestor de descargas o delega en una app externa, y el fragmento se
pierde con frecuencia; las URLs de enclosure llevan 2–3 redirecciones de tracking; y el
enlace secundario a Spotify **no acepta timestamp**. El `#t=` se mantiene como enlace
alternativo para escritorio, y el enlace a la plataforma como "seguir escuchando cómodo".

### 9.1 Publicidad dinámica: el riesgo que condiciona este apartado

Los hostings modernos (Spotify for Podcasters/Anchor, Megaphone, Acast, Art19) hacen
**inserción dinámica de anuncios**: la misma URL devuelve un MP3 distinto en cada descarga,
con cuñas de duración diferente. Dos consecuencias:

1. **`checksum_audio` no detecta ediciones del autor.** Cambia en cada descarga sin que el
   episodio haya cambiado. Se redefine como lo que realmente es: checksum *de la copia
   transcrita*, útil para reproducibilidad local. Para detectar ediciones sirve mejor el par
   (`guid`, `duration_sec` del feed).
2. **Los timestamps derivan.** Se transcribe una copia con 40 s de anuncios y meses después
   se reproduce una con 75 s: todo va desviado 35 s. No da error, solo degrada en silencio
   el valor central del sistema.

Mitigaciones, todas ya incorporadas arriba:

- Persistir `transcribed_duration_sec` junto a `duration_sec`. Su diferencia al reproducir
  es un estimador de la deriva, y permite avisar en la UI.
- Arrancar ~10 s antes del `start_sec` en vez de fingir precisión al segundo.
- **La caché de audio deja de ser un extra**: para episodios marcados como favoritos es la
  única forma de garantizar timestamps exactos. Implementar como opción (local o Supabase
  Storage), pero entendiendo que es eso y no una comodidad.
- Y si se va a diarizar, el audio hace falta de todos modos (§3.3).

---

## 10. Elección de almacenamiento

| Capa | Elección | Motivo |
|------|----------|--------|
| Fuente de verdad | **Git + Markdown** (repo **privado**) | Texto plano, versionable, diff legible, portable, sin lock-in. |
| Índice y búsqueda | **Supabase (Postgres)** | FTS y `pgvector` en el mismo motor → índice híbrido sin sincronizar dos sistemas. |
| Capa de acceso | **Edge Function** | Requisito de §7.5: sin ella el corpus es público. |
| Lectura y navegación manual | **Obsidian** (opcional) | Apunta al mismo directorio del repo. Para leer y enlazar a mano; **no** como motor de búsqueda. |
| Estado del pipeline local | **SQLite** | Cero configuración, transaccional, vive junto al proceso. |

Descartado: índice en SQLite+FTS5+`sqlite-vec` en el Mac. Alternativa válida y muy ligera
**si se prefiere una solución 100% local**, pero rompe el requisito de consultar desde
Android sin depender del portátil encendido.

---

## 11. Estructura del repositorio

```
podcast-kb/                        # repositorio PRIVADO (§7.5)
├── README.md
├── pyproject.toml                 # gestionado con uv (§13.1)
├── config/
│   ├── podcasts.yaml              # fuente de verdad del alta de feeds (§4.1)
│   ├── glossary.es.txt            # prosa, no lista (§5.7)
│   ├── glossary.en.txt
│   ├── fixups.tsv                 # normalización post-whisper
│   ├── topics.yaml                # taxonomía controlada
│   └── speakers.<slug>.yaml       # mapeo de hablantes (§5.11)
├── src/
│   ├── ingest/                    # etapa 1: RSS
│   ├── transcribe/                # etapa 2: whisper
│   ├── diarize/                   # etapa 2b: pyannote (opcional)
│   ├── enrich/                    # etapa 3: LLM
│   ├── export/                    # etapa 4a: emisión de .md
│   └── index/                     # etapa 4b: chunk + embed + carga (§3.2)
├── transcripts/
│   ├── test-de-turing/
│   │   ├── 2026-07-15-agentes-ia-destripando-los-enigmas.md
│   │   └── 2026-07-15-agentes-ia-destripando-los-enigmas.segments.json.gz
│   ├── ia-semanal/
│   └── la-tertul-ia/
├── db/
│   ├── schema.sql                 # Postgres (§7.3)
│   ├── policies.sql               # RLS (§7.5)
│   ├── functions.sql              # hybrid_search (§8.4)
│   └── local.sqlite               # (gitignored)
├── .github/workflows/
│   └── index.yml                  # indexado al hacer push (§3.2)
├── models/                        # (gitignored) ggml-*.bin
├── cache/audio/                   # (gitignored)
└── web/                           # PWA de búsqueda + <audio> (§9)
```

`.gitignore`: `models/`, `cache/`, `db/local.sqlite`, `.env`.

---

## 12. Consideraciones legales y de privacidad

- Las transcripciones son **obras derivadas** de contenido con derechos de autor. El uso
  contemplado es **personal y privado**.
- **No publicar** las transcripciones completas en abierto sin permiso de los autores. Esto
  no es solo una intención: se materializa en el repo privado y en el RLS del §7.5. Si en
  algún momento se quisiera una UI pública, limitar a fragmentos cortos, atribuir
  explícitamente y enlazar siempre al episodio original.
- Respetar el RSS como interfaz prevista de distribución: `User-Agent` propio identificable,
  rate limiting, y respetar `Retry-After` / `429`.
- Los datos salen del equipo local al usar Supabase: **no** volcar ahí material corporativo
  ni credenciales. El proyecto solo maneja contenido público de podcasts.
- Verificar que el uso de recursos y la instalación cumplen la política interna del equipo
  (§13).

### 12.1 Superficie de ataque

Todo lo que el sistema descarga —feed, audio, subtítulos— sale de un RSS de un tercero.
Eso convierte al feed en la entrada no confiable principal, y hay dos consecuencias que
no son evidentes:

- **SSRF.** Un feed hostil, o uno legítimo comprometido, puede apuntar sus enclosures o
  su `<podcast:transcript>` a `169.254.169.254` (metadatos de la nube, si el indexado
  corre en un runner) o a `127.0.0.1` (servicios del portátil). Las URLs se validan
  antes de pedirlas y **en cada redirección**: validar solo la primera no sirve de nada,
  basta redirigir. `PODCAST_KB_ALLOW_PRIVATE_URLS=1` desactiva la comprobación para
  desarrollo, y el `doctor` avisa si está puesta.
- **DNS rebinding.** Validar resolviendo el nombre y dejar que el cliente HTTP lo
  resuelva otra vez al conectar deja una ventana entre las dos resoluciones: un DNS
  hostil contesta una IP pública a la comprobación y `127.0.0.1` a la conexión. Se
  conecta a la **IP ya validada**, mandando el `Host` y el SNI originales, así que el
  certificado se sigue verificando contra el nombre real y no contra la IP. Detrás de un
  proxy no se fija nada —quien resuelve es el proxy, y fijar la IP rompería el
  `CONNECT`—; ahí la defensa es la del proxy. `PODCAST_KB_PIN_DNS=0` lo desactiva.
- **Tamaño.** Nada obliga a un servidor a decir la verdad en `Content-Length`. Las
  descargas van acotadas (32 MB el feed, 16 MB un subtítulo, 1 GB el audio) cortando
  durante la lectura, no confiando en la cabecera. El cuerpo se lee **en streaming**:
  un tope que se comprueba después de haber descargado la respuesta entera en memoria
  no acota nada, que es justo lo que pasaba hasta ahora con el audio.

**El slug del podcast es un nombre de directorio**, así que se valida contra
`^[a-z0-9][a-z0-9-]{0,63}$` al darlo de alta, y la ruta final se comprueba de nuevo
antes de escribir.

**Quién puede buscar.** El RPC se llama con `service_role`, que salta el RLS: quien pase
la autenticación ve todo el corpus. Estar registrado en el proyecto de Supabase no basta
—el registro público está abierto por defecto—, así que la Edge Function exige una lista
explícita en `ALLOWED_EMAILS` y devuelve 503 si no está configurada. Además, en el panel:
desactivar el alta de usuarios y fijar las *Redirect URLs*.

**Lo que se revisó y estaba bien:** el XML de los feeds no es vulnerable a XXE ni a
expansión de entidades (comprobado con ambas cargas); no hay SQL construido por
concatenación; los subprocesos se invocan con lista de argumentos, nunca por shell; y la
UI escapa el contenido del feed —títulos, hablantes y transcripción— sin dejar pasar
inyección de HTML.

**Riesgo aceptado y pendiente:** cuando se implemente el enriquecimiento (§6), el texto
de la transcripción irá a un LLM. Ese texto lo escribe un tercero, así que el prompt debe
tratarlo como dato, no como instrucciones, y la salida debe validarse contra la taxonomía
de `config/topics.yaml` en vez de aceptarse tal cual.

---

## 13. Restricciones del equipo y coste

Restricción operativa: **no se puede instalar software comercial** en el Mac. Python sí está
disponible. Regla de trabajo: **todo lo que cueste dinero se confirma antes de adoptarlo.**

El camino crítico completo es gratuito y open source:

| Pieza | Herramienta | Licencia / coste |
|---|---|---|
| Transcripción | whisper.cpp + pesos de Whisper | MIT / gratis, sin coste por minuto ni cuenta |
| Alternativa a medir | mlx-whisper | MIT / gratis |
| Audio | ffmpeg | LGPL / gratis |
| Runtime | Python + `uv` | PSF, MIT / gratis |
| Embeddings | `multilingual-e5-large`, `bge-m3` | MIT/Apache / gratis |
| Diarización | `pyannote.audio` 3.1 | MIT / gratis (cuenta HF + aceptar licencia del modelo) |
| Enriquecimiento | Ollama / llama.cpp | MIT / gratis |
| Índice | Postgres + pgvector | PostgreSQL, MIT / gratis |
| Repo privado e indexado | GitHub + Actions | gratis (2.000 min/mes) |
| Lectura manual | Obsidian | gratis para uso personal |

Puntos donde existe una opción de pago, todos con sustituto gratuito ya adoptado por
defecto:

| Pieza | Opción de pago | Sustituto en uso |
|---|---|---|
| Episodios problemáticos | API alojada de Whisper (~0,36 USD/h) — no el Whisper local | `large-v3` en local (§5.1) |
| Enriquecimiento (§6) | API de LLM | LLM local 7–14B |
| Embeddings | `voyage-3`, `text-embedding-3-large` | `multilingual-e5-large` local |
| Pausa de Supabase | Plan de pago | Free tier + ping semanal (§8.6) |
| Diarización | pyannoteAI (servicio) | `pyannote.audio` open source |

**El único con coste recurrente que puede valer la pena** es el enriquecimiento del §6:
resumir 350 episodios con una API cuesta del orden de unos pocos euros en total, con mejor
extracción de entidades que un modelo local de 7B. Se planteará como decisión explícita en
la Fase 2, con el coste estimado. Es además la decisión más segura del proyecto para
aplazar: §6 es opcional y re-ejecutable sobre los `.md`, así que empezar en local y cambiar
después no cuesta ninguna retranscripción.

### 13.1 Entorno de ejecución

**Python gestionado con `uv`**: instala su propio intérprete en `~/.local` sin permisos de
administrador ni tocar el Python del sistema, y `uv run` resuelve dependencias por proyecto
de forma reproducible. `venv` + `pip` funciona igual de bien; lo importante es fijarlo en el
`README`.

Evitar depender del Python del sistema de macOS: Apple lo actualiza entre versiones del SO y
rompe entornos sin avisar.

---

## 14. Plan de implementación por fases

Reordenado respecto a v1: **el índice y la UI van antes del backfill masivo.** Motivo: el
backfill son 20–35 h de cómputo, y no tiene sentido gastarlas antes de comprobar que el
formato del `.md`, el tamaño de chunk y el modelo de embeddings producen una búsqueda que se
siente bien. Es exactamente lo que dice el principio rector del §3 ("retranscribir es
caro"), y la v1 lo contradecía en su plan.

### Fase 0 — Cimientos
- [ ] Resolver los 3 `feedUrl` reales vía `itunes.apple.com/lookup` y validar el parseo
      (§2.3), persistiendo la URL final tras redirecciones.
- [ ] Comprobar en cada feed si hay `<podcast:transcript>` / `<podcast:chapters>` (§4.4).
- [ ] Repo **privado** creado, `.gitignore`, esqueleto del CLI con `uv`.
- [ ] Esquema SQLite (§7.1) + `config/podcasts.yaml` (§4.1).
- [ ] `podcast-kb sync --dry-run`: lista episodios detectados sin descargar nada.

### Fase 1 — Camino completo sobre 1 episodio
- [ ] Descarga + normalización con ffmpeg, registrando `transcribed_duration_sec`.
- [ ] whisper.cpp con Metal, `large-v3-turbo`, glosario en prosa, `--vad`, `-oj`
      (**sin `-ml 1`**).
- [ ] Benchmark `whisper.cpp` vs `mlx-whisper` (§5.2) — 20 minutos.
- [ ] Comparativa `turbo` vs `large-v3` **en tramo de habla solapada** (§5.3).
- [ ] Emisión del `.md` + `.segments.json.gz` conforme a §7.2.
- [ ] **Validación con el criterio de §5.10.** Ajustar aquí, no después.
- [ ] `TODO`: decidir timestamps por segmento o por palabra (§5.6).

### Fase 2 — Búsqueda funcionando (con pocos episodios)
- [ ] Transcribir 15–20 episodios (~2 h de máquina).
- [ ] Esquema Postgres + `unaccent` + RLS + pgvector (§7.3, §7.5).
- [ ] Chunking + embeddings + carga incremental por doble checksum (§8.1, §8.5).
- [ ] RPC `hybrid_search` con RRF y diversificación (§8.4).
- [ ] Edge Function + PWA mínima: caja de búsqueda, resultados agrupados por episodio,
      `<audio>` con salto al segundo (§9).
- [ ] **Usarla varios días de verdad** antes de seguir. Aquí es donde se descubre que los
      chunks son largos o que falta un campo en el `.md`.

### Fase 3 — Decisiones que dependen del audio
- [ ] **Decidir diarización antes del backfill** (§3.3, §5.11). Si sí: pyannote + mapeo por
      embeddings de locutor. Si no: decidir si se conserva el WAV.
- [ ] Post-procesado con `fixups.tsv` y detector de repeticiones (§5.8).

### Fase 4 — Volumen
- [ ] Backfill del histórico, en lotes acotados o en máquina con GPU (§5.9, §3.1).
- [ ] Enriquecimiento con LLM (§6) — decisión de proveedor aquí (§13).
- [ ] Indexado automático por GitHub Action (§3.2).

### Fase 5 — Refinamiento
- [ ] Filtros por facetas y taxonomía (§8.4).
- [ ] Caché selectiva de audio para favoritos (§9.1).
- [ ] Automatización con `launchd` (§4.5).
- [ ] `podcast-kb doctor`: verificación de entorno, feeds vivos, episodios en error.

---

## 15. Decisiones pendientes (`TODO`)

| # | Decisión | Cuándo | Nota |
|---|---|---|---|
| 1 | Timestamps por segmento o por palabra | Fase 1 | Afecta al formato de `.segments.json.gz` y a su tamaño (§5.6) |
| 2 | `whisper.cpp` o `mlx-whisper` | Fase 1 | Decidir por benchmark, no por preferencia (§5.2) |
| 3 | `turbo` o `large-v3` | Fase 1 | Decidir en tramo de habla solapada (§5.3) |
| 4 | Modelo de embeddings | Fase 2 | `e5-large` o `bge-m3`; ambos 1024, el esquema no depende (§8.2) |
| 5 | Acceso al backend: Edge Function vs Auth de usuario único | Fase 2 | Sea cual sea, RLS deny-all es innegociable (§7.5) |
| 6 | **Diarizar o no** | **Antes del backfill** | La única decisión que se encarece al posponerla (§3.3) |
| 7 | Proveedor de LLM para enriquecimiento | Fase 4 | Único coste recurrente potencial; reversible (§13) |
| 8 | Dónde corre el indexado: Mac o GitHub Action | Fase 4 | (§3.2) |
| 9 | Política de caché de audio: nada / favoritos / todo | Fase 5 | Condicionada por §9.1 y por el TODO 6 |

---

## 16. Glosario del proyecto

- **Chunk**: fragmento de transcripción (~1–2 min) que constituye la unidad indexada y
  recuperable.
- **Búsqueda híbrida**: combinación de recuperación léxica (coincidencia de términos) y
  semántica (similitud de significado vía embeddings).
- **RRF**: *Reciprocal Rank Fusion*, fusión de rankings basada en posiciones, no en scores.
- **Diarización**: identificación de qué hablante pronuncia cada fragmento.
- **Front matter**: bloque YAML de metadatos al inicio de un fichero Markdown.
- **Enclosure**: elemento del RSS que contiene la URL del fichero de audio del episodio.
- **DAI** (*dynamic ad insertion*): inserción de anuncios en el momento de la descarga, que
  hace que la misma URL devuelva audios distintos (§9.1).
- **VAD** (*voice activity detection*): detección de voz, usada para recortar silencios antes
  de transcribir y evitar alucinaciones (§5.8).
- **RLS** (*row level security*): control de acceso por fila en Postgres. Sin él, las tablas
  de Supabase son públicas (§7.5).
