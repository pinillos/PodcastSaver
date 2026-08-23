-- RLS — §7.5. SIN ESTO EL CORPUS ES PÚBLICO.
-- Generado desde docs/diseno-v2.md. Editar allí y regenerar.

-- Una tabla sin RLS es legible por cualquiera con la anon key, y la anon
-- key va incrustada en el cliente web. La PWA no habla con PostgREST:
-- habla con una Edge Function que valida sesión y usa service_role.

alter table podcasts enable row level security;
alter table episodes enable row level security;
alter table chunks   enable row level security;
-- sin policies: deny-all para anon.
