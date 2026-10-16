"""Backend mínimo para probar, de punta a punta, que un agente real puede
dispararse desde la web: recibe la petición, llama a la API de pago de
Anthropic (no al plan de Claude Code — eso está prohibido fuera de Claude
Code/claude.ai, ver docs/decisiones.md), y deja el resultado en Supabase
para que la web lo lea sola, sondeando directamente la tabla.

Es la prueba de fuego más pequeña posible antes de construir el motor real
de `explorador-segmentos` (que necesita búsqueda web, más complejo). No
reutiliza `orquestador/motor.py` a propósito: ese módulo depende del CLI de
Claude Code instalado localmente, que no existe en un servidor remoto.
"""
from __future__ import annotations

import os

import requests
from anthropic import Anthropic
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

SUPABASE_URL = "https://otokpuowhkpgldfuvgge.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_wjzABwsd3VLNIDjjkd89hg_QLhyQBoF"


def _clave_servicio() -> str:
    clave = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not clave:
        raise RuntimeError("Falta SUPABASE_SERVICE_ROLE_KEY en el entorno del backend.")
    return clave


def _clave_anthropic() -> str:
    clave = os.environ.get("ANTHROPIC_API_KEY")
    if not clave:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en el entorno del backend.")
    return clave


app = FastAPI(title="KDP Intelligence backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # solo lectura/escritura vía claves públicas + RLS, sin datos sensibles aquí
    allow_methods=["*"],
    allow_headers=["*"],
)


def _usuario_desde_token(token: str) -> str:
    """Resuelve el id real de usuario a partir de su token de sesión,
    preguntando a Supabase (no nos fiamos de lo que diga el cliente)."""
    resp = requests.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={"apikey": SUPABASE_PUBLISHABLE_KEY, "Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if resp.status_code != 200:
        raise HTTPException(401, "Sesión no válida.")
    return resp.json()["id"]


def _escribir_job(job_id: str, user_id: str, **campos) -> None:
    cabeceras = {
        "apikey": _clave_servicio(),
        "Authorization": f"Bearer {_clave_servicio()}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    if job_id:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/kdp_jobs?id=eq.{job_id}",
            headers=cabeceras, json=campos, timeout=10,
        )
    else:
        campos["user_id"] = user_id
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/kdp_jobs",
            headers={**cabeceras, "Prefer": "return=representation"},
            json=campos, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()[0]["id"]


def _ejecutar_prueba(job_id: str, user_id: str, pregunta: str) -> None:
    try:
        cliente = Anthropic(api_key=_clave_anthropic())
        respuesta = cliente.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": pregunta}],
        )
        texto = "".join(b.text for b in respuesta.content if b.type == "text")
        _escribir_job(job_id, user_id, estado="completado", salida={"texto": texto}, actualizado_en="now()")
    except Exception as e:  # noqa: BLE001 — se guarda el error tal cual para verlo en la web
        _escribir_job(job_id, user_id, estado="error", error=str(e), actualizado_en="now()")


class PeticionPrueba(BaseModel):
    pregunta: str = "Responde en una frase: ¿qué es KDP Intelligence?"


@app.get("/salud")
def salud():
    return {"ok": True}


@app.post("/prueba")
def crear_prueba(
    peticion: PeticionPrueba,
    background_tasks: BackgroundTasks,
    authorization: str = Header(...),
):
    token = authorization.removeprefix("Bearer ").strip()
    user_id = _usuario_desde_token(token)
    job_id = _escribir_job(None, user_id, tipo="prueba", estado="corriendo", entrada={"pregunta": peticion.pregunta})
    background_tasks.add_task(_ejecutar_prueba, job_id, user_id, peticion.pregunta)
    return {"job_id": job_id}
