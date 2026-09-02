# Edge Function `search`

Capa de acceso entre la PWA y Postgres. Existe por §7.5: una tabla sin RLS es
legible por cualquiera con la `anon key`, y esa clave va incrustada en el
cliente web.

## Variables de entorno

| Variable | Obligatoria | Para qué |
|---|---|---|
| `SUPABASE_URL` | sí | Inyectada por Supabase. |
| `SUPABASE_ANON_KEY` | sí | Validar la sesión del usuario. |
| `SUPABASE_SERVICE_ROLE_KEY` | sí | Llamar al RPC saltando RLS. **Nunca** en el cliente. |
| `EMBEDDING_URL` | no | Endpoint que devuelve el vector de la consulta. Sin ella, la búsqueda es solo léxica. |
| `EMBEDDING_TOKEN` | no | Bearer para ese endpoint. |
| `ALLOWED_EMAILS` | **sí** | Cuentas con acceso, separadas por comas. Sin ella la función rechaza todo con 503. |
| `ALLOWED_ORIGIN` | no | Origen de la PWA. Por defecto `null` (sin CORS). |

## Por qué hace falta `ALLOWED_EMAILS`

El RPC se llama con `service_role`, que salta el RLS: quien pase la
autenticación ve **todo el corpus**. Si el registro público del proyecto está
abierto —lo está por defecto—, cualquiera podría darse de alta y leerlo.

Por eso hay una lista explícita, y por eso sin ella la función devuelve 503 en
vez de abrirse. Complementariamente, en el panel de Supabase:

- **Authentication → Providers → Email**: desactivar «Allow new users to sign up».
- **Authentication → URL Configuration**: fijar la *Site URL* y las *Redirect URLs*
  a la de la PWA, para que un enlace mágico no pueda redirigirse a otro sitio.

## Sobre el embedding

Postgres no ejecuta el modelo, y Deno tampoco puede cargar `multilingual-e5-large`.
El vector se pide a `EMBEDDING_URL`, que puede ser:

- un servicio propio mínimo con `sentence-transformers` (gratis, hay que alojarlo);
- una API de pago (ver §13 del diseño: se confirma antes de adoptarla).

Si no está configurada, o si falla, la función llama al RPC sin embedding y la
fusión RRF degrada a búsqueda léxica pura. La UI sigue funcionando y lo indica.

## Despliegue

```bash
supabase functions deploy search
supabase secrets set EMBEDDING_URL=https://…
```
