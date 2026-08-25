// Autenticación por enlace mágico contra Supabase Auth (§7.5).
//
// Sin sesión no hay búsqueda: la Edge Function rechaza las peticiones sin
// token, que es lo que impide que el corpus sea público. Se usa la API REST
// directamente para no arrastrar el SDK entero a una página que solo
// necesita entrar y guardar un token.

const CLAVE = "podcast-kb.session";

function leer() {
  try {
    const crudo = localStorage.getItem(CLAVE);
    return crudo ? JSON.parse(crudo) : null;
  } catch {
    // Ventana privada o almacenamiento bloqueado: se trata como sin sesión.
    return null;
  }
}

function guardar(sesion) {
  try {
    localStorage.setItem(CLAVE, JSON.stringify(sesion));
  } catch {
    /* sin persistencia: la sesión durará lo que la pestaña */
  }
}

export function cerrarSesion() {
  try {
    localStorage.removeItem(CLAVE);
  } catch {
    /* nada que borrar */
  }
}

/** Recoge el token del fragmento al volver del enlace del correo. */
function recogerDelFragmento() {
  if (!location.hash.includes("access_token")) return null;
  const p = new URLSearchParams(location.hash.slice(1));
  const access_token = p.get("access_token");
  if (!access_token) return null;
  const sesion = {
    access_token,
    refresh_token: p.get("refresh_token"),
    expires_at: Date.now() + (Number(p.get("expires_in") ?? 3600) - 60) * 1000,
  };
  guardar(sesion);
  // Quitar el token de la barra de direcciones: no debe quedar en el
  // historial ni acabar copiado en un enlace compartido.
  history.replaceState(null, "", location.pathname + location.search);
  return sesion;
}

async function refrescar(config, sesion) {
  if (!sesion?.refresh_token) return null;
  try {
    const resp = await fetch(`${config.supabaseUrl}/auth/v1/token?grant_type=refresh_token`, {
      method: "POST",
      headers: { "content-type": "application/json", apikey: config.anonKey },
      body: JSON.stringify({ refresh_token: sesion.refresh_token }),
    });
    if (!resp.ok) return null;
    const datos = await resp.json();
    const nueva = {
      access_token: datos.access_token,
      refresh_token: datos.refresh_token,
      expires_at: Date.now() + (datos.expires_in - 60) * 1000,
    };
    guardar(nueva);
    return nueva;
  } catch {
    return null;
  }
}

/** Token válido, renovándolo si hace falta. `null` si hay que entrar. */
export async function token(config) {
  let sesion = recogerDelFragmento() ?? leer();
  if (!sesion) return null;
  if (sesion.expires_at && sesion.expires_at < Date.now()) {
    sesion = await refrescar(config, sesion);
    if (!sesion) {
      cerrarSesion();
      return null;
    }
  }
  return sesion.access_token;
}

/** Pide el enlace mágico. `create_user: false` para que sea un jardín cerrado. */
export async function enviarEnlace(config, email) {
  const resp = await fetch(`${config.supabaseUrl}/auth/v1/otp`, {
    method: "POST",
    headers: { "content-type": "application/json", apikey: config.anonKey },
    body: JSON.stringify({
      email,
      create_user: false,
      options: { email_redirect_to: location.origin + location.pathname },
    }),
  });
  if (!resp.ok) {
    const detalle = await resp.json().catch(() => ({}));
    throw new Error(detalle.msg ?? detalle.error_description ?? `error ${resp.status}`);
  }
}
