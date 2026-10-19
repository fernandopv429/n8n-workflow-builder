from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config
from ..dependencias import usuario_logado
from ..services import crud

router = APIRouter()
templates = Jinja2Templates(directory=str(config.RAIZ / "app" / "templates"))


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, usuario: dict = Depends(usuario_logado)):
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "usuario": usuario,
            "tarefas": crud.listar_tarefas(usuario, status="pendente"),
            "projetos": crud.listar_projetos(usuario),
            "agora": datetime.now(timezone.utc),
        },
    )


@router.post("/tarefas")
def criar_tarefa_web(
    titulo: str = Form(...),
    projeto_id: str = Form(""),
    prazo: str = Form(""),
    usuario: dict = Depends(usuario_logado),
):
    prazo_dt = datetime.fromisoformat(prazo) if prazo else None
    pid = int(projeto_id) if projeto_id else None
    crud.criar_tarefa(usuario, titulo, prazo=prazo_dt, projeto_id=pid, origem="manual")
    return RedirectResponse("/", status_code=303)


@router.post("/tarefas/{tarefa_id}/concluir")
def concluir_tarefa_web(tarefa_id: int, usuario: dict = Depends(usuario_logado)):
    crud.concluir_tarefa(usuario, tarefa_id)
    return RedirectResponse("/", status_code=303)


@router.post("/projetos")
def criar_projeto_web(nome: str = Form(...), descricao: str = Form(""), usuario: dict = Depends(usuario_logado)):
    crud.criar_projeto(usuario, nome, descricao, origem="manual")
    return RedirectResponse("/", status_code=303)
