// PWA de búsqueda (§9).
//
// El salto temporal se hace aquí, con `audio.currentTime`. Delegar en el
// sistema operativo con un enlace `.mp3#t=` no funciona de forma fiable en
// Android: Chrome lo manda al gestor de descargas o a una app externa, y el
// fragmento se pierde por el camino.

const config = await fetch("config.json").then((r) => r.json()).catch(() => ({}));

// Margen de seguridad: con inserción dinámica de publicidad, la copia que se
// reproduce puede llevar cuñas distintas a la que se transcribió, y los
// timestamps derivan (§9.1). Arrancar antes es más útil que fingir precisión.
const MARGEN_SEG = 10;

const $ = (id) => document.getElementById(id);
const resultados = $("resultados");
const audio = $("audio");

const hhmmss = (s) => {
  const t = Math.max(0, Math.round(s));
  const h = Math.floor(t / 3600);
  const m = String(Math.floor((t % 3600) / 60)).padStart(2, "0");
  const g = String(t % 60).padStart(2, "0");
  return h ? `${h}:${m}:${g}` : `${m}:${g}`;
};

const escapar = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const sinTildes = (s) => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

/** Extracto centrado en la primera coincidencia.
 *
 * Un chunk son ~90 s de audio, unas 200 palabras: la unidad correcta para
 * indexar y para saltar, pero un muro de texto en una pantalla de móvil. Lo
 * que hay que enseñar es el trozo donde está lo que se buscaba.
 */
function extracto(texto, consulta, radio = 110) {
  const terminos = (consulta.match(/[\p{L}\p{N}]{3,}/gu) ?? []).map(sinTildes);
  if (!terminos.length || texto.length <= radio * 2) return { texto, recortado: false };

  const plano = sinTildes(texto);
  let pos = -1;
  for (const t of terminos) {
    const i = plano.indexOf(t);
    if (i !== -1 && (pos === -1 || i < pos)) pos = i;
  }
  if (pos === -1) pos = 0;

  let ini = Math.max(0, pos - radio);
  let fin = Math.min(texto.length, pos + radio);
  // Cortar por espacios para no partir palabras a la mitad.
  if (ini > 0) ini = texto.indexOf(" ", ini) + 1 || ini;
  if (fin < texto.length) fin = texto.lastIndexOf(" ", fin) || fin;

  return {
    texto: (ini > 0 ? "… " : "") + texto.slice(ini, fin).trim() + (fin < texto.length ? " …" : ""),
    recortado: ini > 0 || fin < texto.length,
  };
}

/** Resalta los términos de la consulta, ignorando acentos y mayúsculas. */
function resaltar(texto, consulta) {
  const terminos = consulta.match(/[\p{L}\p{N}]{3,}/gu) ?? [];
  if (!terminos.length) return escapar(texto);
  const objetivo = new Set(terminos.map(sinTildes));
  return texto
    .split(/(\s+)/)
    .map((trozo) => {
      const limpio = sinTildes(trozo.replace(/[^\p{L}\p{N}]/gu, ""));
      const marcar = limpio && [...objetivo].some((t) => limpio.startsWith(t));
      return marcar ? `<mark>${escapar(trozo)}</mark>` : escapar(trozo);
    })
    .join("");
}

async function buscar(consulta) {
  resultados.innerHTML = '<p class="estado">Buscando…</p>';
  const cuerpo = {
    q: consulta,
    podcast_slugs: $("f-podcast").value ? [$("f-podcast").value] : null,
    speaker: $("f-speaker").value.trim() || null,
    date_from: $("f-desde").value || null,
    date_to: $("f-hasta").value || null,
  };

  let datos;
  try {
    const resp = await fetch(`${config.functionsUrl}/search`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${sesion()}`,
      },
      body: JSON.stringify(cuerpo),
    });
    if (resp.status === 401) {
      resultados.innerHTML = '<p class="estado">Sesión caducada. Vuelve a entrar.</p>';
      return;
    }
    datos = await resp.json();
    if (datos.error) throw new Error(datos.error);
  } catch (err) {
    resultados.innerHTML = `<p class="estado">No se pudo buscar: ${escapar(err.message)}</p>`;
    return;
  }

  pintar(datos, consulta);
}

function pintar(datos, consulta) {
  if (!datos.results?.length) {
    resultados.innerHTML = '<p class="estado">Sin resultados.</p>';
    return;
  }
  resultados.replaceChildren(
    ...datos.results.map((ep) => tarjeta(ep, consulta)),
    aviso(datos),
  );
}

function aviso(datos) {
  const p = document.createElement("p");
  p.className = "estado";
  p.textContent = datos.semantic
    ? ""
    : "Búsqueda léxica: la parte semántica está desactivada (falta EMBEDDING_URL).";
  return p;
}

function tarjeta(ep, consulta) {
  const nodo = document.createElement("article");
  nodo.className = "episodio";

  const fecha = ep.published_at ? new Date(ep.published_at).toLocaleDateString("es-ES") : "";
  const numero = ep.episode_number ? ` · Ep. ${ep.episode_number}` : "";
  nodo.innerHTML =
    `<h2>${escapar(ep.title)}</h2>` +
    `<p class="fuente">${escapar(ep.podcast ?? "")}${numero} · ${fecha}</p>`;

  for (const m of ep.moments) {
    const fila = document.createElement("div");
    fila.className = "fila";

    const { texto, recortado } = extracto(m.content, consulta);
    const quien = m.speaker ? `<span class="quien">${escapar(m.speaker)}: </span>` : "";

    const boton = document.createElement("button");
    boton.className = "momento";
    boton.innerHTML =
      `<span class="sello">${hhmmss(m.start_sec)}</span>` +
      `<span class="texto">${quien}${resaltar(texto, consulta)}</span>`;
    boton.addEventListener("click", () => reproducir(ep, m));
    fila.appendChild(boton);

    if (recortado) {
      const mas = document.createElement("button");
      mas.className = "mas";
      mas.textContent = "ver el fragmento completo";
      mas.addEventListener("click", () => {
        boton.querySelector(".texto").innerHTML = quien + resaltar(m.content, consulta);
        mas.remove();
      });
      fila.appendChild(mas);
    }
    nodo.appendChild(fila);
  }
  return nodo;
}

function reproducir(ep, momento) {
  const panel = $("reproductor");
  if (!ep.audio_url) {
    panel.hidden = false;
    $("p-titulo").textContent = ep.title;
    $("p-aviso").hidden = false;
    $("p-aviso").textContent = "Este episodio no tiene audio accesible.";
    return;
  }

  const desde = Math.max(0, momento.start_sec - MARGEN_SEG);
  if (audio.dataset.episodio !== ep.episode_id) {
    audio.src = ep.audio_url;
    audio.dataset.episodio = ep.episode_id;
    // El <audio> nace con preload="none" para no bajar 60 MB al abrir la
    // página. Pero entonces asignar `src` no dispara nada, así que
    // `loadedmetadata` nunca llegaría y el salto no ocurriría: hay que pedir
    // la carga explícitamente.
    audio.preload = "metadata";
    audio.load();
  }

  const saltar = () => {
    audio.currentTime = desde;
    audio.play().catch(() => {});
  };
  if (audio.readyState >= 1) saltar();
  else audio.addEventListener("loadedmetadata", saltar, { once: true });

  panel.hidden = false;
  $("p-titulo").textContent = ep.title;
  $("p-momento").textContent = hhmmss(desde);
  $("p-aviso").hidden = true;
}

$("cerrar").addEventListener("click", () => {
  audio.pause();
  $("reproductor").hidden = true;
});

$("buscador").addEventListener("submit", (e) => {
  e.preventDefault();
  const consulta = $("q").value.trim();
  if (consulta) {
    history.replaceState(null, "", `?q=${encodeURIComponent(consulta)}`);
    buscar(consulta);
  }
});

function sesion() {
  // Supabase Auth guarda la sesión en localStorage. Se lee de forma
  // defensiva: en una ventana privada el acceso puede lanzar.
  try {
    const clave = Object.keys(localStorage).find((k) => k.endsWith("-auth-token"));
    return clave ? JSON.parse(localStorage.getItem(clave)).access_token : "";
  } catch {
    return "";
  }
}

// Rellenar el selector de podcasts desde la configuración.
for (const p of config.podcasts ?? []) {
  const opcion = document.createElement("option");
  opcion.value = p.slug;
  opcion.textContent = p.title;
  $("f-podcast").appendChild(opcion);
}

// Permitir enlazar una búsqueda concreta.
const inicial = new URLSearchParams(location.search).get("q");
if (inicial) {
  $("q").value = inicial;
  buscar(inicial);
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}
