-- RPC de búsqueda híbrida — §8.3, §8.4
-- Generado desde docs/diseno-v2.md. Editar allí y regenerar.

-- El embedding de la consulta se calcula FUERA (Edge Function).
-- Si no se pasa, la búsqueda es solo léxica.

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
