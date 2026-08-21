---
proyecto: podcast-kb
titulo: "Revisión de viabilidad del diseño v1"
revisa: docs/diseno-v1.md
fecha: 2026-08-21
estado: revisión técnica
---

# Revisión del diseño v1 — viabilidad, correcciones y mejoras

## 0. Veredicto

**El proyecto es viable y la arquitectura es la correcta.** La decisión rectora —el Mac
produce artefactos de texto plano y todo lo demás es reconstruible desde ellos— es la
decisión buena, y es la que hace que los errores del resto del documento sean baratos de
corregir: casi todo lo que señalo aquí se arregla reindexando, no retranscribiendo.

Los riesgos reales no están donde el documento los pone. El documento se preocupa mucho
del *throttling* térmico (§5.2, riesgo menor y gestionable) y no menciona los cinco puntos
que de verdad pueden hundir el sistema:

1. La decisión más importante del proyecto —`whisper.cpp`— se justifica con una premisa
   falsa (que no se puede usar Python). La elección resulta ser correcta de todos modos,
   pero por otras razones, y la premisa falsa arrastra tres decisiones más.
2. El corpus acabaría siendo **público** por omisión (Supabase sin RLS, repo Git sin
   marcar como privado), lo que contradice frontalmente el §12.
3. La **inserción dinámica de publicidad** rompe a la vez `checksum_audio` y la precisión
   del salto temporal, que es *el* valor del sistema.
4. El enlace `#t=` no funciona de forma fiable en el caso de uso principal (Android).
5. El índice léxico está declarado pero nunca se puebla: `chunks.tsv` es una columna que
   hay que rellenar a mano en cada `INSERT`, y una columna que se olvida de poblar es un
   índice que devuelve cero resultados sin dar error.

Ninguno es bloqueante si se corrige antes de escribir código. Abajo van ordenados por
coste de corregirlos tarde.

> **Nota de verificación.** El esquema y el RPC de este documento se han ejecutado contra
> un Postgres 16 real (con `pgvector` simulado, que aquí no está disponible). Eso confirmó
> el bug del cast a `regconfig` (B.6) y las pruebas funcionales de B.5, y **refutó una de
> mis afirmaciones**: ver A.5, rectificada.

También hay un **error de flag** que produciría una transcripción inservible en la Fase 1
(§A.0), y una recomendación de modelo desactualizada (§D.1).

---

## A. Correcciones (cosas que están mal, no opiniones)

### A.0 `-ml 1` es un error y rompería la Fase 1

§5.4 propone:

```bash
whisper-cli -m models/ggml-medium.bin -f ep.wav -l es --prompt "<glosario>" -oj -ml 1
```

En whisper.cpp, `-ml N` / `--max-len N` es **longitud máxima de segmento en caracteres**,
no en segundos ni en frases. `-ml 1` pide segmentos de un carácter: el decodificador parte
en cada token y produce timestamps a nivel de token. Eso contradice directamente el §7.2,
que describe "segmentos crudos de whisper (a menudo de 3–8 s)" — con `-ml 1` no habría
tales segmentos, el `.segments.json` sería 5–10× más grande y el agrupado en bloques de
30–60 s habría que rehacerlo desde cero.

**Corrección**: eliminar `-ml 1` (el default `-ml 0` da la segmentación natural de ~3–8 s
que el documento asume).

```bash
whisper-cli -m models/ggml-large-v3-turbo-q5_0.bin -f ep.wav -l es \
  --prompt "$(cat config/glossary.es.txt)" -oj -of out/ep -pp false
```

Si en algún momento se quiere timestamp por palabra —que para saltar al minuto exacto es
un lujo real— la forma correcta es **deliberada y documentada**: `-ml 1 -sow` (split on
word). Pero entonces hay que decidirlo en §7.2 y asumir el tamaño del JSON, no meterlo de
tapadillo en un ejemplo.

### A.1 La justificación del §5.2 parte de una premisa falsa

§5.2 justifica `whisper.cpp` con un argumento explícito: *"binario compilado sin
dependencias pesadas de Python → encaja con un equipo corporativo donde no se puede
instalar libremente"*.

**La premisa es incorrecta.** En el Mac se puede usar Python sin problema. La restricción
real es distinta —no se puede instalar **software comercial**— y tiene consecuencias
diferentes (§H).

Esto no cambia la conclusión, pero sí cuatro cosas alrededor de ella. Y hay que corregir el
texto igualmente: una decisión apoyada en una premisa falsa se revierte en cuanto alguien
detecta la premisa, aunque la decisión fuera buena.

**a) `whisper.cpp` sigue siendo el default correcto, por otras razones.** No por evitar
Python, sino por: aceleración Metal, VAD integrado (C.7), cuantización `q5_0`/`q8_0` para
ajustar RAM, y madurez. Reescribir la justificación del §5.2 en esos términos.

**b) `mlx-whisper` entra en la ecuación y merece medirse.** Es la implementación de Whisper
sobre MLX, el framework de Apple para Apple Silicon: Python, `pip install mlx-whisper`,
open source, ejecuta en la GPU integrada. En M-series suele competir de tú a tú con
whisper.cpp y a menudo lo supera con modelos grandes. El documento no lo menciona porque lo
descartó implícitamente al descartar Python.

No propongo cambiar el default a ciegas. Propongo **medirlo en la Fase 1**: es un
`pip install` y una transcripción de 10 minutos, y la diferencia se multiplica por las ~260
horas del backfill (D.2). Si gana, se gana un fin de semana; si pierde, se han gastado 20
minutos. Contrapartida conocida: mlx-whisper no trae VAD integrado (habría que añadir
`silero-vad` por separado), y ahí whisper.cpp lleva ventaja de comodidad.

**c) Cuidado con `faster-whisper` en Mac — el §5.2 induce a error.** El documento lo ofrece
como *"alternativa si se permite Python (...), también viable en CPU"*. Ahora que Python sí
está permitido, es la opción a la que se tiende de forma natural, y en este equipo concreto
es la peor de las tres: CTranslate2 **no tiene backend Metal**, así que en Mac corre solo en
CPU. En un Air sin ventilador eso significa renunciar a la GPU y multiplicar los tiempos de
D.2. `faster-whisper` es excelente en máquinas con CUDA; aquí no. Conviene decirlo en §5.2
en vez de presentarlo como equivalente.

**d) §5.7 y §8.2 dejan de estar bloqueadas.** Era la parte que yo daba por contradictoria y
no lo es:

- **Diarización** (§5.7): `pyannote.audio` es viable en el Mac (torch sobre MPS). Esto
  cambia su prioridad — ver D.4, donde propongo adelantarla.
- **Embeddings** (§8.2): `multilingual-e5-large` o `bge-m3` vía `sentence-transformers`
  corren en local sin fricción. Ya no hace falta la vuelta por `llama.cpp` que yo sugería.

Con esto, **dónde se calculan los embeddings deja de ser una restricción y pasa a ser una
decisión de arquitectura**, que es como debe tratarse. Mi recomendación se mantiene, pero
el argumento es otro: ponerlos en la etapa `src/index/` y no en `src/export/`. Motivo — el
principio rector del §3 dice que el `.md` es el contrato entre las dos mitades. Si los
embeddings se generan en la exportación, el Mac pasa a ser necesario para reindexar; si se
generan en la indexación, reindexar solo necesita el repo. Y como el repo es Git, eso
habilita algo que el documento no contempla: **ejecutar la indexación en un GitHub Action
disparado por `push`**, con el Mac apagado. Es gratis en repos privados y encaja
exactamente con el desacople del §3.

### A.2 El corpus sería público por omisión

§12 dice, correctamente, "no publicar las transcripciones completas en abierto sin permiso
de los autores". El diseño técnico hace justo lo contrario por defecto, en dos sitios:

1. **Supabase**: una tabla sin *Row Level Security* es legible por cualquiera con la
   `anon key`, y la `anon key` va incrustada en el cliente web —es pública por diseño—. Un
   proyecto Supabase con el esquema del §7.3 tal cual publica el corpus entero en internet.
2. **El repo Git**: §11 describe la estructura pero nunca dice que el repositorio deba ser
   privado. `transcripts/` versionado en un repo público es exactamente la publicación que
   §12 prohíbe.

**Corrección** (añadir a §7.3 y §11):

```sql
alter table podcasts enable row level security;
alter table episodes enable row level security;
alter table chunks   enable row level security;
-- sin policies: deny-all para anon. El acceso va por Edge Function
-- con service_role del lado servidor, o por Supabase Auth de usuario único.
```

Y la UI no habla con PostgREST directamente: habla con una Edge Function que valida sesión
y llama al RPC `hybrid_search`. Es una línea más de arquitectura y evita el único fallo del
proyecto que tiene consecuencias fuera del portátil.

El repo, privado. Explícitamente, en §11.

### A.3 Inserción dinámica de publicidad (DAI) — el riesgo no contemplado

Ni una mención en todo el documento, y afecta a dos campos del contrato de datos.

Los hostings modernos (Spotify for Podcasters/Anchor, Megaphone, Acast, Art19...) hacen
**inserción dinámica**: la misma URL de enclosure devuelve un MP3 **distinto en cada
descarga**, con cuñas diferentes y de duración diferente, insertadas en pre/mid-roll.
Consecuencias directas sobre el diseño:

- **`checksum_audio` (§7.2, §9) no sirve para lo que dice servir.** El documento lo
  justifica como "para poder detectar re-subidas o ediciones del episodio". Con DAI cambia
  en cada descarga sin que el episodio haya cambiado: es un detector de falsos positivos.
- **El salto temporal se desalinea.** Se transcribe una copia con 40 s de anuncios y el
  usuario, meses después, abre una copia con 75 s: todos los timestamps van desviados 35 s.
  Es el fallo más insidioso porque no da error, solo degrada silenciosamente el valor
  central del sistema.

**Correcciones**:

- Redefinir `checksum_audio` como lo que realmente es: checksum *de la copia transcrita*,
  útil para reproducibilidad local, no para detectar ediciones del autor. Para eso último
  sirve mejor el par (`guid`, `duration_sec` del feed).
- Persistir `transcribed_duration_sec` (medida sobre el WAV real que se transcribió) junto
  a `duration_sec` (la que declara el feed). La diferencia entre ambas al reproducir es un
  estimador de la deriva.
- En la UI, presentar el salto como aproximado (arrancar ~10 s antes del `start_sec`) en
  vez de fingir precisión al segundo. Cuesta nada y cubre la deriva típica.
- Para episodios marcados como favoritos, la caché de audio del §9 deja de ser un extra y
  pasa a ser **la única forma de garantizar timestamps exactos**. Vale la pena decirlo así
  en §9, porque cambia la prioridad de esa función.

### A.4 `#t=` no resuelve el caso de uso principal

§9 apuesta todo a `https://.../episode.mp3#t=<segundos>`, "soportado por navegadores y por
la mayoría de reproductores nativos". En un navegador de escritorio con un `<audio>`
propio, sí. En el caso de uso que el documento declara prioritario —buscar desde Android—
es poco fiable:

- Un enlace a un `.mp3` desde Chrome en Android abre el gestor de descargas o delega en
  una app externa; el fragmento `#t=` se pierde con frecuencia.
- Las URLs de enclosure suelen llevar prefijos de tracking (`podtrac`, `chartable`,
  `pdst.fm`) con 2–3 redirecciones antes del fichero real.
- El enlace secundario a Spotify/YouTube que propone el documento **no acepta timestamp**
  en el caso de Spotify.

**Corrección**: el reproductor mínimo en la propia PWA no es un lujo de la Fase 4, es lo
que hace funcionar la Fase 3. Un `<audio src={audio_url}>` y `audio.currentTime = start`
en el `onClick` del resultado. Son ~20 líneas y convierten el resultado de búsqueda en algo
utilizable. El *deep link* externo queda como secundario, para "seguir escuchando cómodo".

Esto contradice el no-objetivo del §1.1 ("no se construye reproductor propio en v1"). Es el
no-objetivo el que está mal planteado: nadie pide un reproductor de podcasts completo, se
pide un `<audio>` que empiece en el segundo correcto.

### A.5 ~~El índice léxico ignora los acentos~~ — RECTIFICADO: era falso

**Este hallazgo era incorrecto y lo retiro.** Afirmé que el stemmer `spanish` de Postgres no
quita tildes y que por tanto `sesion` no encontraría `sesión`. Al ejecutarlo contra un
Postgres 16 real resulta que **sí las quita**: el algoritmo Snowball para español elimina los
acentos agudos en su fase final, así que la inmensa mayoría de las palabras ya coinciden sin
tilde. La configuración `spanish` que proponía la v1 es correcta.

Medición, `to_tsvector('spanish', a) @@ plainto_tsquery('spanish', b)`:

| Texto → consulta | `spanish` | Con `unaccent` |
|---|---|---|
| sesión → sesion | ✅ | ✅ |
| código → codigo | ✅ | ✅ |
| atención → atencion | ✅ | ✅ |
| parámetros → parametros | ✅ | ✅ |
| Álvaro → Alvaro | ✅ | ✅ |
| Ávila → Avila | ✅ | ✅ |
| Mollá → Molla | ✅ | ✅ |
| Martín → Martin | ✅ | ✅ |
| María → Maria | ❌ | ✅ |
| además → ademas | ❌ | ✅ |

El residuo que `unaccent` sí arreglaría (`María`, `además`) viene de **stemming asimétrico**:
el stemmer aplica las reglas de sufijo sobre la forma acentuada y sobre la no acentuada de
manera distinta, y ambas convergen en lexemas diferentes. Es real, pero marginal — y los
nombres propios acentuados del corpus (Álvaro, Mollá, Ávila, Martín) funcionan bien sin
`unaccent`.

**Y `unaccent` tiene un coste que no había considerado: colapsa la ñ.**

| | `spanish` los distingue | `es_unaccent` los distingue |
|---|---|---|
| año vs ano | ✅ | ❌ |
| cañón vs canon | ✅ | ❌ |
| campaña vs campana | ✅ | ❌ |
| sueño vs sueno | ✅ | ❌ |

La ñ es una letra, no una tilde, y `unaccent` la trata como si lo fuera. En un corpus en
español donde "año", "diseño", "campaña" o "español" aparecen constantemente, eso es una
pérdida de precisión a cambio de una ganancia de recall casi nula.

En un Postgres propio se podría cargar un fichero de reglas `unaccent` personalizado que
preserve la ñ, pero **en Supabase gestionado no se puede** escribir en
`$SHAREDIR/tsearch_data/`, así que esa escapatoria no existe aquí.

**Conclusión rectificada**: usar `spanish` / `english` tal como proponía la v1, sin
`unaccent`. El TODO #5 del §14 se resuelve a favor de lo que el documento original ya decía.

Lo que **sí** sobrevive de este apartado es un detalle menor y real: consultar con
`websearch_to_tsquery` en vez de `plainto_tsquery`, porque acepta comillas y `-exclusión`,
que es lo que un humano teclea sin pensar.

### A.6 `chunks.tsv` está declarado pero nunca se puebla

§7.3 declara `tsv tsvector` y §7.3 (nota final) dice "en la ingesta, según
`episodes.language`". Eso significa mantenerlo a mano en cada `INSERT` y en cada
reindexado, y una columna que se olvida de poblar es un índice que devuelve cero
resultados sin dar error.

Lo natural sería una columna generada, pero **no se puede** hacer una columna generada cuya
configuración de idioma dependa de otra fila: `to_tsvector(regconfig, text)` no es
`IMMUTABLE` con configuración dinámica. Dos salidas limpias:

**Opción recomendada** — dos columnas generadas con índices parciales:

```sql
alter table chunks
  add column tsv_es tsvector generated always as (to_tsvector('spanish', content)) stored,
  add column tsv_en tsvector generated always as (to_tsvector('english', content)) stored,
  add column language text not null;   -- denormalizado, ver B.1

create index chunks_tsv_es_idx on chunks using gin (tsv_es) where language = 'es';
create index chunks_tsv_en_idx on chunks using gin (tsv_en) where language = 'en';
```

Cuesta algo de espacio (un `tsvector` vacío en la columna que no aplica es barato) y elimina
toda una clase de bugs: no hay forma de insertar un chunk sin índice léxico.

**Alternativa** si el espacio importa: un trigger `BEFORE INSERT OR UPDATE` que puebla `tsv`
con la configuración del idioma. Menos declarativo, misma garantía.

Esto resuelve además el TODO #5 del §14: la respuesta es `spanish`/`english` por idioma
—sin `unaccent`, ver A.5— y la duda desaparece. Verificado: las columnas generadas se crean
sin problema, porque `to_tsvector` con un nombre de configuración **literal** sí es
`IMMUTABLE`; lo que no lo es es pasarle una configuración dinámica.

---

## B. Mejoras de esquema e índice

### B.1 Filtrar sobre HNSW destruye el recall si se hace por JOIN

§8.4 pide filtrar por podcast, fechas, idioma, `topics[]`, `speaker`. Con el esquema actual
esos campos viven en `episodes` y el vector en `chunks`, así que el filtro es un JOIN
posterior a la búsqueda vectorial. Eso es **post-filtrado**: HNSW devuelve sus 50 vecinos
globales, el JOIN se come 45, y la consulta *"menciones a MCP en El Test de Turing durante
2026"* —el ejemplo literal del §8.4— devuelve 5 resultados mediocres o ninguno.

**Corrección**: denormalizar en `chunks` los campos por los que se filtra —`podcast_id`,
`published_at`, `language`— y activar el escaneo iterativo de pgvector (≥ 0.8):

```sql
set hnsw.iterative_scan = relaxed_order;
set hnsw.max_scan_tuples = 20000;
```

La denormalización es correcta aquí: son campos inmutables por episodio y `chunks` se
reconstruye entero desde el `.md` cuando cambian. El coste de mantenimiento es cero.

### B.2 `halfvec` en vez de `vector` — el free tier de Supabase da más justo de lo que parece

Estimación con los números del propio documento (chunk de ~1,5 min, solape 20% → ~50
chunks por hora de audio):

| Concepto | Por episodio (~45 min) | 350 episodios (backfill) |
|---|---|---|
| Vectores `vector(1024)` (4 B/dim) | ~150 KB | ~52 MB |
| Índice HNSW (m=16) | ~110 KB | ~38 MB |
| Texto de chunks + `tsvector` ×2 | ~90 KB | ~31 MB |
| **Total aproximado** | **~350 KB** | **~120 MB** |

Cabe en los 500 MB del *free tier*, pero con menos margen del que da la sensación —una vez
sumados WAL, bloat y un reindexado completo en caliente, un backfill grande se acerca
incómodamente al límite. `halfvec(1024)` (pgvector ≥ 0.7, `halfvec_cosine_ops`) reduce
vectores e índice a la mitad con pérdida de recall despreciable a esta escala:

```sql
embedding halfvec(1024)
create index chunks_embedding_idx on chunks using hnsw (embedding halfvec_cosine_ops);
```

Advertencia operativa aparte: un proyecto Supabase en *free tier* se **pausa** tras un
periodo de inactividad. Para un sistema personal que se consulta a ráfagas, eso significa
esperas al despertar y, si se olvida durante meses, restaurar a mano. Conviene decidirlo
conscientemente (plan de pago, o un *ping* semanal desde el propio `launchd` del Mac).

### B.3 Un solo `md_checksum` obliga a re-embeder por cambios de metadatos

§8.5 guarda `md_checksum` del fichero entero. Pero el `.md` mezcla dos cosas con ciclos de
vida muy distintos: el **front matter** (que cambia cada vez que se re-ejecuta el
enriquecimiento, §6, explícitamente diseñado como re-ejecutable) y el **cuerpo de la
transcripción** (que solo cambia si se retranscribe o se aplican `fixups`).

Con un checksum único, re-enriquecer 350 episodios para probar un prompt nuevo invalida
17.500 chunks y dispara un re-embedding completo que no hacía ninguna falta.

**Corrección**: dos checksums.

- `transcript_checksum` — hash del cuerpo tras el `---` de cierre. Decide re-chunk +
  re-embed.
- `meta_checksum` — hash del front matter. Decide un `UPDATE` barato sobre `episodes`.

### B.4 Cachear embeddings por hash de texto

Complementa a B.3 y cubre el caso que B.3 no cubre: cuando se ajusta el tamaño de chunk o
el solape, la mayoría del texto sigue siendo idéntico. Una tabla local
`embeddings_cache(text_sha256 PRIMARY KEY, model, embedding)` en el SQLite del Mac —o una
tabla en Postgres— convierte un recorte de parámetros en una operación gratis. Con
embeddings por API, además, esto es dinero directo.

### B.5 RRF: acotar cada rama y diversificar por episodio

§8.3 define bien la fórmula pero no los detalles que deciden si la búsqueda se siente bien:

- Acotar **cada rama a K=50–100** antes de fusionar. Sin tope, la rama léxica de una
  consulta con un término frecuente devuelve miles de filas y la fusión se vuelve lenta y
  peor.
- **Diversificar por episodio**: sin esto, una consulta sobre "agentes" devuelve los 10
  chunks solapados del mismo bloque de 15 minutos del mismo episodio. Agrupar por
  `episode_id`, quedarse con los 2–3 mejores chunks de cada uno, y presentar el resultado
  como *episodio + los momentos relevantes dentro de él*. Esto además encaja mucho mejor
  con lo que el usuario busca: un episodio al que saltar, no un párrafo suelto.
- Deduplicar por solape temporal: dos chunks con solape del 20% sobre el mismo momento son
  el mismo resultado presentado dos veces.

---

### B.6 Dos bugs del RPC que solo aparecen al ejecutarlo

Al escribir la v2 del diseño y pasarla por un Postgres 16 real salieron dos fallos que no se
ven leyendo el SQL:

**a) `websearch_to_tsquery` exige `regconfig`, no `text`.** Elegir la configuración con un
`CASE` produce `text` y la función no resuelve:

```
ERROR: function websearch_to_tsquery(text, text) does not exist
```

Hace falta un cast explícito: `(case when lang='en' then 'english' else 'spanish' end)::regconfig`.

**b) Una CTE compartida para los filtros desactiva el índice vectorial.** Lo natural es
factorizar los filtros comunes en un `with filtered as (...)` que usen las dos ramas. Pero
una CTE referenciada **más de una vez no se inlinea** en Postgres: se materializa. La rama
vectorial recibiría entonces una tabla materializada y el planificador **no podría usar el
índice HNSW** para el `order by embedding <=> q`, degradando la consulta a un escaneo
secuencial de todo el corpus.

Hay que duplicar las condiciones de filtro en cada rama. Es feo y es lo correcto.

---

## C. Omisiones

### C.1 Podcasting 2.0: transcripciones y capítulos que quizá ya existen

§6 aprovecha `<description>` / `<itunes:summary>`, lo cual está bien, pero el documento
nunca menciona los elementos del espacio de nombres `podcast:` que resuelven parte del
problema **gratis**:

- **`<podcast:transcript>`** — algunos feeds publican ya la transcripción en SRT/VTT/JSON.
  Cuando existe, suele ser mejor que la de Whisper (a menudo revisada) y cuesta una
  descarga en vez de 5 minutos de GPU.
- **`<podcast:chapters>`** — capítulos en JSON con timestamps, exactamente el campo
  `chapters[]` del §6, sin LLM.
- `<psc:chapters>` — el formato antiguo, todavía frecuente.

**Añadir al pipeline** un paso previo a la transcripción: si el ítem trae
`<podcast:transcript>`, descargarlo, convertirlo al formato de `.segments.json` y saltarse
Whisper (marcando `transcript.engine: "feed"` en el front matter, para poder distinguirlo
después). Aunque solo lo tenga uno de los tres podcasts, el coste de comprobarlo es
trivial y el ahorro es de horas de máquina.

### C.2 No hay decisión sobre el lenguaje de implementación

El §14 lista siete decisiones pendientes y **no incluye la más estructurante**: en qué se
escribe el CLI. Todo el documento habla de esquemas y flags sin decir nunca si `src/ingest/`
es Python, Go o TypeScript.

Confirmado que Python está disponible en el Mac, **la respuesta es Python** y conviene
escribirlo en el documento, no dejarlo implícito: es lo que hace viables `feedparser`,
`pyannote`, `sentence-transformers` y `mlx-whisper` sin discusión, y es donde vive el
ecosistema entero de este dominio.

**Recomendación de gestión de entorno: `uv`.** Instala su propio intérprete en `~/.local`
sin tocar el Python del sistema ni pedir permisos de administrador, y `uv run` resuelve
dependencias por proyecto de forma reproducible. Es open source (MIT), así que no toca la
restricción de §H. La alternativa —`venv` + `pip` a pelo— funciona igual de bien; lo que
importa es fijarlo en el `README` para que el entorno sea reproducible.

Lo que sí hay que evitar es depender del Python del sistema de macOS: Apple lo actualiza
entre versiones del SO y rompe entornos sin avisar.

### C.3 Dedupe entre feeds: los episodios de La Tertul-IA salen en dos sitios

Hallazgo de la verificación (§E): los episodios de La Tertul-IA se publican **también**
dentro del feed de *Growth: el podcast de Product Hackers* (Apple `id1236733939`, Spreaker
show `4955285`). Es el mismo audio con `guid` distinto y en un `podcast_id` distinto.

El esquema del §7.2 deduplica con `UNIQUE (podcast_id, guid)`, que por construcción **no
puede** detectar esto: son dos podcasts distintos para el sistema. Si algún día se da de
alta el feed de Growth —y es tentador, porque tiene más contenido— se transcribiría dos
veces el mismo audio y la búsqueda devolvería resultados duplicados.

**Corrección barata**: guardar `enclosure_sha256` (hash de la URL normalizada: sin query
de tracking, sin prefijos de redirección) con un índice **global**, no por podcast, y
comprobarlo antes de descargar. Detecta el caso sin coste añadido.

### C.4 La máquina de estados no contempla errores ni indexado

§4.2 define `discovered → downloaded → transcribed → enriched → exported → published` y
dice que una ejecución interrumpida se retoma donde se quedó. Le faltan tres cosas para
que eso sea cierto:

- **Estados de fallo con reintento**: la tabla tiene una columna `error TEXT` pero ningún
  `attempts` ni `next_retry_at`. Un episodio cuyo audio da 404 se reintentaría en cada
  `sync`, para siempre, sin *backoff*.
- **`indexed`**: el estado que representa "cargado en Supabase" no existe. `published` es
  ambiguo —no se dice en ninguna parte qué significa— y sospecho que quiere decir esto.
  Renombrarlo a `indexed` y eliminar `published`.
- **Transiciones no lineales**: `enriched` puede rehacerse sobre un episodio ya `indexed`
  (§6 dice que la etapa es re-ejecutable). Una máquina de estados lineal no lo modela.
  Más honesto: un estado de progreso más flags/timestamps independientes
  (`transcribed_at`, `enriched_at`, `indexed_at`), que es lo que la propia tabla ya
  insinúa.

### C.5 `podcasts.yaml` y SQLite: dos fuentes de verdad sin regla de precedencia

§11 describe `config/podcasts.yaml` como "alta declarativa de feeds (fuente para SQLite)"
y §4.1 da de alta podcasts por CLI (`podcast-kb add`), que escribe en SQLite. Son dos
caminos hacia el mismo estado y el documento no dice cuál gana si divergen.

**Regla propuesta**: el YAML manda y está versionado; SQLite es caché derivada + estado de
ejecución. `podcast-kb add` **escribe en el YAML** y luego reconcilia; `sync` reconcilia al
arrancar. Así el alta de podcasts queda en Git, que es coherente con el resto del diseño.

### C.6 Tamaño del repo Git

§7.2 acierta al conservar los segmentos crudos ("el JSON es la materia prima"), pero no
estima el coste. Con segmentación natural (~3–8 s), 1 h de audio produce ~150–300 KB de
JSON; con timestamps por palabra, 0,5–1 MB. Para 350 episodios de backfill: entre 50 MB y
350 MB de JSON en Git, y el JSON comprime mal en *delta* entre commits porque cada fichero
es nuevo.

No es catastrófico, pero es evitable: guardar `.segments.json.gz` (relación ~5:1 en JSON
con texto repetitivo). Los `.md` se quedan sin comprimir, que es lo que da sentido a
versionarlos en Git —el diff legible del §10—; el JSON nunca se lee a mano.

### C.7 Alucinaciones de Whisper — el fallo de calidad más probable

El documento cubre bien el vocabulario (§5.5) y nada de los dos fallos de Whisper que más
van a aparecer en podcasts:

- **Alucinación en silencio/música**: sintonías, cortinillas y colas de episodio producen
  texto inventado. En español, el clásico es *"Subtítulos realizados por la comunidad de
  Amara.org"* y variantes de despedida ("Gracias por ver el vídeo"), aprendidas de los
  subtítulos de YouTube del entrenamiento.
- **Bucles de repetición**: una frase repetida decenas de veces cuando el audio se
  degrada.

**Mitigaciones**, por orden de efectividad:

1. **VAD**, ya soportado por whisper.cpp (`--vad --vad-model models/ggml-silero-v5.1.2.bin`).
   Recorta los silencios antes de decodificar y elimina la mayoría de las alucinaciones de
   raíz. Es la palanca grande y el documento no la menciona.
2. Añadir las frases fantasma conocidas a `config/fixups.tsv` (que ya existe en el diseño).
3. Un **detector de n-gramas repetidos** en el post-proceso que marque el episodio como
   `needs_review` en vez de publicarlo silenciosamente. Sin esto no hay forma de enterarse
   de que un episodio salió mal salvo leyéndolo.

### C.8 Falta criterio de aceptación en la Fase 1

§13, Fase 1, dice "Validación humana de la calidad de transcripción antes de escalar". Sin
un criterio, esa casilla se marca por cansancio.

**Protocolo concreto**: tomar 3 tramos de 3 minutos (uno de cada podcast, elegidos en
zonas de conversación cruzada), contar (a) errores en términos del glosario y (b) frases
incomprensibles. Umbral de paso: <2 errores de glosario por tramo y 0 alucinaciones. Si no
pasa, subir de `turbo` a `large-v3` antes de tocar el glosario —es la palanca de mayor
efecto y la más barata de probar.

---

## D. Ajustes de recomendación

### D.1 `medium` está desactualizado como modelo por defecto

§5.2 ordena la calidad como `small` < `medium` < `large-v3` y recomienda `medium`. Esa
escala ignora **`large-v3-turbo`** (809M parámetros, decoder reducido de 32 a 4 capas), que
es del orden de 5–8× más rápido que `large-v3` y de calidad claramente superior a `medium`
en español. En Apple Silicon con Metal es hoy el punto dulce real, no `medium`.

**Recomendación**: `ggml-large-v3-turbo-q5_0.bin` (~570 MB) como default, `large-v3`
completo como escalón para episodios que fallen la validación de C.8.

**Matiz importante para este corpus concreto**: la degradación conocida de `turbo` se
concentra en audio ruidoso y con **habla solapada** —que es exactamente lo que pasa en una
tertulia a tres voces, dos de los tres podcasts—. Por eso la Fase 1 debe comparar
`turbo` contra `large-v3` **en un tramo de tertulia con gente hablando encima**, no en un
monólogo limpio. Es una prueba de 20 minutos que decide horas de cómputo del backfill.

Esto responde también al TODO #7 del §14.

### D.2 Poner números al presupuesto térmico y de tiempo

§5.2 advierte del *throttling* sin cuantificarlo, lo que hace imposible planificar el
backfill. Órdenes de magnitud en un Air M2/M3 con Metal:

| Modelo | Factor tiempo real | 1 h de audio | Backfill ~350 eps (~260 h audio) |
|---|---|---|---|
| `medium-q5_0` | ~5–8× | 8–12 min | 35–50 h |
| `large-v3-turbo-q5_0` | ~8–15× | 4–8 min | 18–33 h |
| `large-v3-q5_0` | ~2–3× | 20–30 min | 85–130 h |

Con *throttling* sostenido en un chasis sin ventilador, restar entre un 20% y un 30% a
esas cifras en lotes largos. Conclusiones prácticas:

- `turbo` no es una optimización opcional: es la diferencia entre un backfill de un fin de
  semana y uno de dos semanas.
- Ejecutar con `caffeinate -i` para que la suspensión no corte los lotes.
- Lotes nocturnos de `--max-episodes 10` con el portátil sobre superficie dura.
- Si el Mac es corporativo, plantearse hacer **el backfill una sola vez** en otra máquina
  (un PC con GPU hace esas 260 h en unas 3–5 h) y dejar al Air solo el incremental de 3
  episodios semanales, que son ~20 min de máquina a la semana. El diseño ya permite esto
  gratis gracias al desacople del §3 — merece la pena decirlo explícitamente.

### D.3 El glosario, redactado como prosa y no como lista

§5.5 propone un glosario que es una lista de términos separados por comas. Dos problemas
conocidos del *initial prompt* de Whisper:

- Con una lista sin contexto gramatical, Whisper a veces **la reproduce literalmente** al
  principio de la transcripción (el prompt es el contexto previo simulado: si el "contexto
  previo" es una lista, el modelo continúa la lista).
- El efecto del prompt **decae** conforme avanza el episodio: condiciona sobre todo las
  primeras ventanas de 30 s, no el minuto 40.

**Ajustes**:

1. Redactarlo como una frase natural del dominio: *"En este episodio hablamos de LLMs,
   embeddings, fine-tuning, RAG, agentes, MCP, context window y quantization, con modelos
   de OpenAI, Anthropic, Google y Mistral."* Mismo vocabulario, forma que el modelo puede
   continuar sin copiar.
2. Descartar en el post-proceso el primer segmento si su similitud con el prompt supera un
   umbral.
3. Asumir que la palanca de verdad para la consistencia a lo largo del episodio es
   `fixups.tsv`, no el prompt. El diseño ya lo tiene previsto (bien); solo hay que ordenar
   la expectativa: el prompt ayuda al principio, los fixups arreglan el resto.

### D.4 Diarización: viable ya, y conviene adelantarla

§5.7 la aplaza a "fase 2" —en la práctica Fase 4 según §13— porque la daba por costosa de
montar. Con Python disponible en el Mac esa razón desaparece: `pyannote.audio` corre sobre
torch/MPS en Apple Silicon, es open source y gratuito (requiere cuenta en Hugging Face y
aceptar la licencia del modelo `speaker-diarization-3.1`, que es gratis; **no** confundir con
el servicio de pago pyannoteAI, que sí quedaría bajo §H).

**Recomiendo adelantarla a la Fase 2, antes del backfill masivo.** No por capricho: la
diarización se aplica sobre el **audio**, y el audio es justo lo que el diseño decide no
conservar (§9). Si se diariza después del backfill, hay que **volver a descargar 260 horas
de audio** — y con inserción dinámica de publicidad (A.3), ese audio ya no es el mismo que
se transcribió, así que los timestamps de la diarización no cuadrarán con los de la
transcripción. Es el único punto del diseño donde postergar algo lo vuelve caro en vez de
barato, y contradice el principio del §3.

Si aun así se prefiere no diarizar todavía, la alternativa es **guardar el WAV normalizado**
de cada episodio hasta que se decida (unos 100 MB/hora a 16 kHz mono; ~26 GB para el
backfill completo, borrables después). Feo, pero mucho más barato que redescargar.

Sobre el mapeo de hablantes, §5.7 identifica bien el problema real —"el orden no es estable
entre episodios"— y propone "un mapa por podcast", lo que en la práctica significa etiquetar
a mano `SPEAKER_00 → Frankie` en cada episodio. Para 350 episodios eso no se va a hacer.

**Mejor solución**: extraer una vez 15–20 s de voz limpia de cada tertuliano, calcular su
*embedding* de locutor, y asignar cada cluster del episodio al tertuliano más cercano por
similitud de coseno. Se etiqueta una vez por podcast, no una vez por episodio, y sobrevive
a los cambios de orden. Con invitados, el cluster que no se parezca a nadie se queda como
`SPEAKER_XX` — que es exactamente el comportamiento deseable.

El campo `speaker` reservado en el esquema (§7.3) ya soporta esto sin migración: la
decisión de §5.7 de reservarlo desde el día uno es acertada y no hay que tocarla.

### D.5 Reordenar Fase 2 y Fase 3

§13 pone el backfill completo (Fase 2) antes del índice y la búsqueda (Fase 3). Es el orden
equivocado en términos de riesgo: invierte 20–50 h de cómputo y varios días de reloj
**antes** de haber comprobado que el formato del `.md`, el tamaño de chunk y el modelo de
embeddings producen una búsqueda que se siente bien.

**Reordenación propuesta**: tras la Fase 1, transcribir **15–20 episodios** (unas 2 h de
máquina), montar con ellos el índice, el RPC y la UI mínima, y usarla de verdad durante
unos días. Ahí es donde se descubre que los chunks son demasiado largos, que hace falta
diversificar por episodio (B.5) o que el `.md` necesita otro campo. Solo entonces lanzar
el backfill masivo.

Esto no retrasa nada —el backfill es tiempo de máquina desatendido— y protege contra el
único error caro del proyecto: retranscribir. Encaja además con el principio rector del
§3, que dice exactamente esto ("retranscribir es caro") pero cuyo plan de fases lo
contradice.

---

## E. Verificación de las fuentes (§2)

### E.1 Resuelto: identidad del podcast #3 (cierra el TODO #6)

**Confirmado que el #3 es el correcto y que el homónimo es otro.** El podcast Apple
`id1723736263` — *La Tertul-IA: Inteligencia Artificial y más* — está presentado por Lu
Martín, Frankie Carrero y Corti, del equipo de AI Hackers (proyecto vinculado a Product
Hackers), con newsletter en `tertulia.mumbler.io`. Coincide con los autores del documento.
También está en iVoox como `f12478692`.

Es distinto de *La TERTULia de la Inteligencia Artificial* (ironbar.github.io, Apple
`id1669083682`), tal como advertía §2.1. El aviso de desambiguación era correcto y puede
marcarse como resuelto.

**Hallazgo adicional** (origen de C.3): los mismos episodios se publican dentro de *Growth:
el podcast de Product Hackers* (Apple `id1236733939`, Spreaker show `4955285`), numerados
como "La Tertul-IA #NN". Dos feeds, mismo audio, `guid` distintos.

### E.2 No verificado: las tres `feedUrl` reales

**No he podido resolverlas.** La política de egress de este entorno bloquea
`itunes.apple.com`, `www.ivoox.com` y `anchor.fm`, así que el TODO #1 del §14 sigue
abierto. Los comandos, para ejecutarlos en el Mac:

```bash
for id in 1723256857 1771978939 1723736263; do
  curl -s "https://itunes.apple.com/lookup?id=$id&entity=podcast" \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)["results"][0]; print(r["collectionName"],"|",r.get("feedUrl"))'
done
```

Pistas para cuando se ejecute:

- **#1 Inteligencia Artificial Semanal**: distribuido por iVoox (`f12364979`). Los feeds de
  iVoox siguen el patrón `https://www.ivoox.com/feed_fg_f<ID>_filtro_1.xml`. Conviene
  contrastar ese patrón con el `feedUrl` que devuelva Apple y quedarse con el canónico de
  Apple.
- **#2 El Test de Turing**: `https://anchor.fm/s/e1671d44/podcast/rss` es un patrón válido
  de Spotify for Podcasters. Verificar que sigue respondiendo 200 y no un redirect a un
  dominio `spotify.com` (Spotify ha ido migrando dominios); guardar la URL final, no la
  inicial.
- **#3 La Tertul-IA**: resolver por Apple `id1723736263`. Si Apple devuelve el feed de
  Mumbler o de iVoox, dar de alta ese, no el de Growth (C.3).

**Comprobación adicional recomendada al validar cada feed** — mirar si el ítem trae
`<podcast:transcript>` o `<podcast:chapters>` (C.1). Un `grep` basta y puede ahorrar el
backfill entero:

```bash
curl -s "$FEED" | grep -oE '<podcast:(transcript|chapters)[^>]*>' | head
```

---

## F. §14 revisado — decisiones pendientes

Decisiones que el documento tenía y quedan **resueltas**:

| # | Decisión | Resolución |
|---|---|---|
| 5 | Configuración de `tsvector` | `spanish` / `english` por idioma, **sin `unaccent`** — como decía la v1 (A.5, medido) |
| 6 | Identidad del podcast #3 | Confirmada, es el correcto (E.1) |
| 7 | Tamaño del modelo Whisper | `large-v3-turbo` por defecto; validar contra `large-v3` en tramo de tertulia (D.1) |

Decisiones que **siguen abiertas**, reformuladas:

| # | Decisión | Nota |
|---|---|---|
| 1 | Modelo de embeddings | `multilingual-e5-large` o `bge-m3`; ambos 1024 dims, así que el esquema no depende de la elección. Ambos gratuitos y locales (A.1.d). Decidir en Fase 3, no antes |
| 2 | Proveedor de LLM para enriquecimiento | Única decisión del proyecto con coste recurrente real. Ver §H |
| 3 | UI propia vs cliente ligero | Condicionada por A.2 (hace falta una capa servidor de todos modos) y A.4 (hace falta un `<audio>` propio) |
| 4 | Política de caché de audio | Reformulada por A.3: la caché es la única garantía de timestamps exactos, no un extra. Y por D.4: si se diariza, el audio hace falta de todos modos |

Decisiones **nuevas** que hay que tomar y no estaban:

| # | Decisión | Cuándo |
|---|---|---|
| 8 | **Dónde corre la etapa de indexado**: Mac, o GitHub Action disparado por `push` (A.1.d) | Fase 3 |
| 9 | **Modelo de acceso al backend**: RLS + Edge Function vs Auth de usuario único (A.2) | Fase 3, pero decidir ya |
| 10 | **Repo privado** y política de qué se versiona (A.2, C.6) | Fase 0 |
| 11 | **Timestamps por segmento o por palabra** (A.0) | Fase 1 — afecta al formato de `.segments.json` |
| 12 | **Si se diariza o no** — y si no, si se conserva el WAV por si acaso (D.4) | Antes del backfill, no después |

Resuelta y fuera de la lista: el **lenguaje de implementación** es Python, gestionado con
`uv` (C.2).

---

## H. Restricción real del equipo: software comercial

La restricción operativa del Mac no es Python (A.1), sino que **no se puede instalar
software comercial**. Es una restricción distinta y con otro alcance, así que conviene
inventariar dónde toca el diseño. Regla de trabajo acordada: **caso por caso** — todo lo que
cueste dinero, instalado o en la nube, se confirma antes de adoptarlo.

La buena noticia es que el camino crítico entero es gratuito y open source. Nada de lo que
hace falta para llegar a una búsqueda funcionando cuesta un euro:

| Pieza | Herramienta | Licencia / coste |
|---|---|---|
| Transcripción | whisper.cpp + pesos de Whisper | MIT / gratis, sin coste por minuto ni cuenta |
| Alternativa a medir (A.1.b) | mlx-whisper | MIT / gratis |
| Audio | ffmpeg | LGPL / gratis |
| Runtime y entorno | Python + `uv` | PSF, MIT / gratis |
| Embeddings | `multilingual-e5-large`, `bge-m3` | MIT/Apache / gratis |
| Diarización | `pyannote.audio` 3.1 | MIT / gratis (cuenta HF + aceptar licencia del modelo) |
| Índice y búsqueda | Postgres + pgvector | PostgreSQL, MIT / gratis |
| Repo privado | GitHub | gratis para repos privados |
| Indexado desatendido | GitHub Actions | gratis en repo privado (2.000 min/mes) |
| Lectura manual | Obsidian | gratis; se mantiene (decisión tomada) |

Y los puntos donde el diseño **sí** roza algo de pago. Ninguno es necesario, todos tienen
sustituto gratuito, y los planteo aquí para preguntarlos cuando toque en vez de asumirlos.

> **No confundir las dos cosas que se llaman "Whisper".** El modelo es open source (MIT):
> ejecutarlo en local es gratis, ilimitado y sin cuenta, y es el camino por defecto de todo
> este diseño. Lo que cuesta dinero es la **API alojada** de OpenAI, que sirve el mismo
> modelo desde sus servidores. Solo aparece abajo porque el §5.1 la menciona como plan B, y
> mi recomendación es no usarla: `large-v3` en local es gratis y normalmente mejor, porque
> la API sirve `large-v2` en la mayoría de los casos.

| Pieza del diseño | Opción de pago | Sustituto gratuito | Cuándo lo preguntaré |
|---|---|---|---|
| §5.1 plan B para episodios problemáticos | API **alojada** de Whisper (OpenAI, ~0,36 USD/h) — no el Whisper local | `large-v3` completo en local: más lento, sin coste, y probablemente mejor | Solo si algún episodio falla la validación de C.8 |
| §6 enriquecimiento con LLM | API de LLM | LLM local (`Ollama`/`llama.cpp`, 7–14B): suficiente para resumen y extracción de entidades sobre texto ya transcrito | Fase 2, al implementar §6 |
| §8.2 embeddings | `voyage-3`, `text-embedding-3-large` | `multilingual-e5-large` local, misma dimensión | No hace falta; descartado salvo que lo pidas |
| B.2 pausa por inactividad de Supabase | Plan de pago | Free tier + `ping` semanal desde el `launchd` que ya existe (§4.3) | Fase 3, si la pausa molesta en la práctica |
| D.4 diarización | pyannoteAI (servicio) | `pyannote.audio` open source, mismo linaje | No hace falta |

**El único con coste recurrente que puede valer la pena** es el enriquecimiento (§6):
resumir 350 episodios con una API cuesta del orden de unos pocos euros en total, y la
calidad frente a un modelo local de 7B es notablemente mejor en extracción de entidades. Lo
plantearé como pregunta concreta cuando lleguemos a la Fase 2, con el coste estimado
delante. Hasta entonces, el diseño asume LLM local.

Nota sobre §6 que refuerza esto: el documento ya dice que el enriquecimiento es opcional
para la v1 y re-ejecutable sobre los `.md`. Eso significa que la decisión se puede aplazar
sin coste **y** revertir después: si se empieza con un modelo local y no convence, se
reprocesa con API sin retranscribir nada. Es el mejor sitio posible para dejar una decisión
abierta.

---

## I. Cambios concretos al documento, en orden

Si solo se aplican cinco cosas, que sean estas:

1. **§5.4**: quitar `-ml 1`. Cambiar el modelo por defecto a `large-v3-turbo`. Añadir `--vad`.
2. **§7.3 / §11**: RLS deny-all, acceso vía Edge Function, repo privado.
3. **§7.3**: columnas generadas `tsv_es`/`tsv_en` con `spanish`/`english`, y denormalizar
   `podcast_id`/`published_at`/`language` en `chunks` para que el filtrado no destruya el
   recall de HNSW.
4. **§9 / §1.1**: reproductor `<audio>` mínimo en la PWA dentro del alcance de la v1;
   redefinir `checksum_audio` y añadir `transcribed_duration_sec` por la publicidad dinámica.
5. **§13**: mover la Fase 3 (índice + UI con 20 episodios) por delante del backfill completo,
   y decidir la diarización **antes** del backfill, no después (D.4).

Y dos correcciones de texto que no cambian ninguna decisión pero evitan que se reviertan
por el motivo equivocado: reescribir la justificación del §5.2 (A.1) y marcar
`faster-whisper` como mala opción *en Mac* concretamente (A.1.c).
