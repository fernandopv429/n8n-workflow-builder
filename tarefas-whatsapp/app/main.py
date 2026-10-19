import logging

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .database import garantir_schema
from .routers import auth, painel, reunioes, whatsapp
from .services import scheduler

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Tarefas e Projetos via WhatsApp")
app.add_middleware(SessionMiddleware, secret_key=config.obrigatorio("SESSION_SECRET"))

app.include_router(auth.router)
app.include_router(painel.router)
app.include_router(whatsapp.router)
app.include_router(reunioes.router)

_scheduler = None


@app.on_event("startup")
def iniciar():
    global _scheduler
    garantir_schema()
    _scheduler = scheduler.iniciar()


@app.get("/saude")
def saude():
    return {"status": "ok"}
