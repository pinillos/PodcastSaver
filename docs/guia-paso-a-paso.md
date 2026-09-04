# Guía paso a paso

Dos formas de leerla, con el mismo contenido:

- **En el navegador**, con casillas para marcar por dónde vas:
  https://claude.ai/code/artifact/d58b736d-4d79-4478-a942-38b4b02ffab8
- **`docs/guia.html`**: fichero autocontenido. Se abre con doble clic, funciona
  sin conexión y se puede enviar por correo. No pide nada a internet: las
  tipografías van empotradas dentro del propio fichero.

Incluye un diagrama de qué hace cada equipo y qué datos cruzan entre ellos.

Escrita para seguirse sin conocimientos técnicos. Cubre desde instalar las
herramientas hasta buscar desde el móvil, con los tiempos reales de cada paso
—distinguiendo el tuyo del del ordenador— y qué hacer cuando algo falla.

Si la editas, edita el artefacto y vuelve a publicarlo sobre la misma URL para
que no se separen.

## Resumen de las fases

| Fase | Qué se hace | Tiempo |
|---|---|---|
| A | Instalar Homebrew, ffmpeg, Whisper, uv y los modelos | ~30 min, casi todo esperando |
| B | Primer episodio transcrito de punta a punta | ~15 min |
| C | Las tres pruebas que deciden modelo y motor | ~30 min, una sola vez |
| D | Decidir si se separan las voces | 5 min de pensar |
| E | Transcribir el archivo completo | 34–64 h de ordenador |
| F | Publicar la búsqueda para el móvil | ~1 h |

## Los comandos, en una tabla

| Comando | Para qué |
|---|---|
| `podcast-kb doctor` | Comprueba entorno y despliegue. Empieza siempre por aquí |
| `podcast-kb init` | Crea el estado local |
| `podcast-kb resolve` | Localiza y valida los feeds |
| `podcast-kb sync` | Busca episodios nuevos |
| `podcast-kb episodes` | Qué hay y en qué etapa está |
| `podcast-kb process N` | Transcribe un episodio |
| `podcast-kb process --pending 15` | Transcribe los 15 pendientes más recientes |
| `podcast-kb bench tramo.wav` | Compara motores y modelos |
| `podcast-kb index` | Sube las transcripciones al buscador |

Casi todos aceptan `--dry-run` para mirar sin tocar nada.
