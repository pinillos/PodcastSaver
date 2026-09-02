"""Configuración común de los tests.

Los tests usan `httpx.MockTransport` con hosts que no existen, y servidores
locales en 127.0.0.1. La validación anti-SSRF de `net` bloquea ambas cosas —
que es justo lo que debe hacer en producción—, así que aquí se desactiva.

Los tests que comprueban el bloqueo lo reactivan explícitamente.
"""

import os

os.environ.setdefault("PODCAST_KB_ALLOW_PRIVATE_URLS", "1")
