"""Motor real de `explorador-segmentos` para el backend web: usa la API de
Anthropic directamente (con la herramienta nativa `web_search`), no el CLI
de Claude Code.

Las instrucciones del agente, las reglas de evidencia y el esquema están
**copiados aquí a mano**, no leídos del repositorio en tiempo real: este
backend se despliega desde una carpeta separada de otro repositorio
(`kdp-intelligence-backend/` en `Bots-Trading`, ver `backend/README.md`) que
no tiene acceso al resto de `kdp-intelligence`. Si se cambia
`.claude/agents/explorador-segmentos.md`, `CLAUDE.md`,
`prompts/semilla-momentos-vitales.md` o `schemas/segmento.json`, hay que
actualizar también esta copia a mano.

Versión reducida a propósito para la primera prueba real (ver
docs/decisiones.md): menos candidatos y menos búsquedas que el modo
`candidatos` completo (40-60 candidatos), para que la primera ejecución de
verdad sea barata y rápida de comprobar.
"""
from __future__ import annotations

import json
import re

REGLAS_EVIDENCIA = """Reglas de evidencia de KDP Intelligence (de CLAUDE.md):
1. Toda cifra o afirmación importante se guarda con: url, fuente, fecha de consulta, tipo \
(observado, estimado o hipotesis) y confianza (alta, media o baja).
2. No inventes ventas, búsquedas, ingresos ni conversiones. Si un dato no existe, escribe \
"no disponible".
3. El interés de búsqueda, el número de reseñas y la posición en ventas no son ventas.
4. Si una cifra importante depende de una sola fuente, márcala como tal.
5. No rastrees ni automatices Amazon por tu cuenta. Usa la búsqueda web y páginas públicas \
normales."""

SEMILLA_MOMENTOS_VITALES = """Semilla de momentos vitales (punto de partida para la vía B, \
no un límite; los grupos con (*) son de riesgo alto):
- Familia y crianza: primer hijo, adolescentes, pantallas y primer móvil, hermanos, hijos con \
necesidades específicas, conciliar trabajo y familia.
- Pareja y relaciones: convivencia, separación, vuelta a empezar, amistades en la edad adulta.
- Trabajo y carrera: cambio de empleo, paro de larga duración, teletrabajo, oposiciones, \
emprender, jefes difíciles, volver a trabajar tras un parón.
- Dinero (*): deudas, ahorro, jubilación, herencias, finanzas de pareja.
- Salud y bienestar (*): sueño, ansiedad, dolor crónico, menopausia, hábitos, cuidar a alguien \
enfermo.
- Envejecer y cuidar: cuidar a un familiar mayor, soledad, tecnología para mayores, vivir solo.
- Hogar y vivienda: alquiler, compra, reformas, mudanza, convivir con compañeros de piso.
- Mascotas: primer perro, conducta, adopción, convivencia entre animales.
- Tecnología cotidiana: uso de la IA en el día a día, privacidad, estafas en internet, trámites \
digitales.
- Estudios y aprendizaje: idiomas, adultos que vuelven a estudiar, técnicas de estudio, \
preparación de exámenes.
- Trámites y burocracia (*): autónomos, impuestos, reclamaciones, ayudas públicas.
- Cambios vitales: divorcio, duelo, jubilación, vivir en otro país, ser cuidador.
- Ocio y aficiones: aficiones con curva de aprendizaje dura, proyectos manuales, viajes con \
condiciones especiales."""

ESQUEMA_SEGMENTO = """Cada candidato, como objeto JSON:
{
  "id": "s01, s02... (correlativo)",
  "nombre": "string",
  "quien": "string",
  "situacion": "string",
  "via_origen": "A_mercado | B_dolor | C_web",
  "notas": {"demanda": 0-10, "dolor": 0-10, "disposicion_pago": 0-10, "oferta_mejorable": 0-10, "alcance": 0-10},
  "evidencias_por_senal": {"demanda": ["solo la URL, sin descripción"], ...},
  "lagunas": ["qué no se ha podido comprobar"],
  "riesgo": "bajo | medio | alto",
  "ambito_sensible": true/false,
  "confianza": "alta | media | baja"
}
No incluyas "puntuacion": la calcula el código, no tú."""

SYSTEM_PROMPT = f"""Eres el explorador de segmentos de KDP Intelligence. Tu trabajo es \
encontrar segmentos de lectores en España con un problema real y señales de mercado para libros \
en Amazon.es, empezando de cero.

Un segmento es "quién + en qué situación", concreto (ej. "personas que cuidan a un padre con \
demencia y siguen trabajando"). Nunca una categoría vacía como "salud" o "mascotas".

{REGLAS_EVIDENCIA}

{SEMILLA_MOMENTOS_VITALES}

{ESQUEMA_SEGMENTO}

Reglas:
- No prioricéis algo solo porque esté de moda en redes sociales.
- Si no encuentras evidencia para una señal, pon nota 0, confianza baja, y anótalo en "lagunas". \
No rellenes con suposiciones.
- Marca ambito_sensible=true y riesgo=alto en salud, finanzas, legislación y seguridad.
- Una oferta vacía no es una oportunidad: puede significar que nadie quiere comprar. Dilo si lo ves.

Sé breve: nombre y situación en una frase corta cada uno, evidencias solo como URL (sin texto \
descriptivo), lagunas en 3-4 palabras.

Al terminar, devuelve ÚNICAMENTE un bloque de código ```json con una lista de los candidatos \
(ningún texto antes o después del bloque)."""


def construir_tarea(num_candidatos: int, max_busquedas: int) -> str:
    return f"""Genera {num_candidatos} candidatos de segmento distintos entre sí, usando las vías \
A (mercado), B (dolor, a partir de la semilla) y C (web/tendencias), equilibrando entre ellas. \
Tienes como máximo {max_busquedas} búsquedas en total — como mucho una búsqueda por candidato. \
Si llegas al límite, cierra con lo que tengas.

Devuelve la lista completa en el formato indicado, dentro de un bloque ```json."""


def extraer_json(texto: str) -> list[dict]:
    """Saca la lista de candidatos del texto final del modelo. Busca primero
    un bloque ```json cerrado; si el bloque se cortó (respuesta truncada por
    max_tokens), quita la valla de apertura y recorta hasta el último objeto
    completo de la lista, en vez de fallar con toda la respuesta perdida."""
    m = re.search(r"```json\s*(.*?)\s*```", texto, re.DOTALL)
    if m:
        return json.loads(m.group(1))

    bruto = re.sub(r"^```json\s*", "", texto.strip())
    try:
        return json.loads(bruto)
    except json.JSONDecodeError:
        ultimo_cierre = bruto.rfind("}")
        if ultimo_cierre == -1:
            raise
        return json.loads(bruto[: ultimo_cierre + 1].rstrip(",\n ") + "\n]")
