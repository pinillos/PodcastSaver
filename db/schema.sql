-- Esquema del backend (Supabase / Postgres) — §7.3
-- Generado desde docs/diseno-v2.md. Editar allí y regenerar.

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
