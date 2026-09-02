// Edge Function de búsqueda (§7.5, §8.4).
//
// La PWA NO habla con PostgREST: sin RLS y con la anon key incrustada en el
// cliente, el corpus entero sería público, que es justo lo que el §12 prohíbe.
// Esta función valida la sesión y llama al RPC con service_role del lado
// servidor.
//
// El embedding de la consulta se calcula aquí porque Postgres no ejecuta el
// modelo. Si EMBEDDING_URL no está configurada, se llama al RPC sin embedding
// y la búsqueda degrada a léxica pura, que sigue siendo útil.

import { createClient } from "jsr:@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const EMBEDDING_URL = Deno.env.get("EMBEDDING_URL");
const EMBEDDING_TOKEN = Deno.env.get("EMBEDDING_TOKEN");

// Jardín cerrado: solo estas cuentas pueden buscar. Estar autenticado en el
// proyecto de Supabase NO basta —si el registro público está abierto,
// cualquiera se daría de alta y leería el corpus entero—. Sin la variable
// puesta no entra nadie: es preferible un fallo visible a una fuga silenciosa.
const ALLOWED_EMAILS = new Set(
  (Deno.env.get("ALLOWED_EMAILS") ?? "")
    .split(",")
    .map((e) => e.trim().toLowerCase())
    .filter(Boolean),
);

const CORS = {
  "Access-Control-Allow-Origin": Deno.env.get("ALLOWED_ORIGIN") ?? "null",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

interface SearchBody {
  q?: string;
  lang?: string;
  podcast_slugs?: string[];
  date_from?: string | null;
  date_to?: string | null;
  speaker?: string | null;
  topics?: string[] | null;
  limit?: number;
  max_per_episode?: number;
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "content-type": "application/json" },
  });
}

/** e5 exige el prefijo `query:`; usarlo solo en un lado degrada el recall. */
async function embedQuery(text: string): Promise<number[] | null> {
  if (!EMBEDDING_URL) return null;
  try {
    const resp = await fetch(EMBEDDING_URL, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(EMBEDDING_TOKEN ? { authorization: `Bearer ${EMBEDDING_TOKEN}` } : {}),
      },
      body: JSON.stringify({ input: `query: ${text}` }),
      signal: AbortSignal.timeout(5000),
    });
    if (!resp.ok) return null;
    const payload = await resp.json();
    return payload.embedding ?? payload.data?.[0]?.embedding ?? null;
  } catch {
    // Que el servicio de embeddings esté caído degrada la búsqueda a léxica,
    // no la rompe.
    return null;
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "método no permitido" }, 405);

  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ")) {
    return json({ error: "falta la sesión" }, 401);
  }

  // La sesión se valida con la anon key; el RPC se llama con service_role.
  const anon = createClient(SUPABASE_URL, Deno.env.get("SUPABASE_ANON_KEY")!, {
    global: { headers: { Authorization: authHeader } },
  });
  const { data: { user }, error: authError } = await anon.auth.getUser();
  if (authError || !user) return json({ error: "sesión no válida" }, 401);

  if (ALLOWED_EMAILS.size === 0) {
    console.error("ALLOWED_EMAILS no está configurada: se rechaza todo.");
    return json({ error: "servicio no configurado" }, 503);
  }
  if (!user.email || !ALLOWED_EMAILS.has(user.email.toLowerCase())) {
    return json({ error: "esta cuenta no tiene acceso" }, 403);
  }

  let body: SearchBody;
  try {
    body = await req.json();
  } catch {
    return json({ error: "cuerpo JSON no válido" }, 400);
  }

  const q = (body.q ?? "").trim();
  if (!q) return json({ error: "consulta vacía" }, 400);
  // Una consulta enorme se convierte en una tsquery enorme, y si hay
  // EMBEDDING_URL de pago, en dinero.
  if (q.length > 500) return json({ error: "consulta demasiado larga" }, 400);

  const admin = createClient(SUPABASE_URL, SERVICE_ROLE_KEY);

  let podcastIds: string[] | null = null;
  if (body.podcast_slugs?.length) {
    const { data } = await admin.from("podcasts").select("id").in("slug", body.podcast_slugs);
    podcastIds = (data ?? []).map((row: { id: string }) => row.id);
  }

  const embedding = await embedQuery(q);

  const { data, error } = await admin.rpc("hybrid_search", {
    q,
    q_embedding: embedding ? `[${embedding.join(",")}]` : null,
    lang: body.lang ?? "es",
    podcast_ids: podcastIds,
    date_from: body.date_from ?? null,
    date_to: body.date_to ?? null,
    topics_filter: body.topics ?? null,
    speaker_filter: body.speaker ?? null,
    max_per_episode: body.max_per_episode ?? 3,
    result_limit: Math.min(body.limit ?? 20, 50),
  });
  if (error) return json({ error: error.message }, 500);

  const rows = data ?? [];
  const episodeIds = [...new Set(rows.map((r: { episode_id: string }) => r.episode_id))];
  const { data: episodes } = await admin
    .from("episodes")
    .select("id, title, episode_number, published_at, audio_url, episode_url, duration_sec, podcast_id")
    .in("id", episodeIds);
  const { data: podcasts } = await admin.from("podcasts").select("id, slug, title");

  const podcastById = new Map((podcasts ?? []).map((p: any) => [p.id, p]));
  const episodeById = new Map((episodes ?? []).map((e: any) => [e.id, e]));

  // Agrupado por episodio: lo que se busca es un sitio al que saltar, no un
  // párrafo suelto (B.5).
  const grouped = episodeIds.map((id) => {
    const episode = episodeById.get(id);
    const podcast = episode ? podcastById.get(episode.podcast_id) : null;
    const moments = rows
      .filter((r: any) => r.episode_id === id)
      .map((r: any) => ({
        start_sec: r.start_sec,
        end_sec: r.end_sec,
        speaker: r.speaker,
        content: r.content,
        score: r.score,
      }));
    return {
      episode_id: id,
      title: episode?.title ?? "(desconocido)",
      episode_number: episode?.episode_number ?? null,
      published_at: episode?.published_at ?? null,
      duration_sec: episode?.duration_sec ?? null,
      audio_url: episode?.audio_url ?? null,
      episode_url: episode?.episode_url ?? null,
      podcast: podcast?.title ?? null,
      podcast_slug: podcast?.slug ?? null,
      score: Math.max(...moments.map((m: any) => m.score)),
      moments,
    };
  }).sort((a, b) => b.score - a.score);

  return json({ query: q, semantic: embedding !== null, results: grouped });
});
