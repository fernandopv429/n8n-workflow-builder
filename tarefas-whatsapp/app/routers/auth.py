from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config
from ..services import crud

router = APIRouter()
templates = Jinja2Templates(directory=str(config.RAIZ / "app" / "templates"))


@router.get("/login", response_class=HTMLResponse)
def tela_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "usuario": None, "erro": None})


@router.post("/login")
def fazer_login(request: Request, email: str = Form(...), senha: str = Form(...)):
    usuario = crud.autenticar(email, senha)
    if not usuario:
        return templates.TemplateResponse(
            "login.html", {"request": request, "usuario": None, "erro": "email ou senha incorretos"}
        )
    request.session["usuario_id"] = usuario["id"]
    return RedirectResponse("/", status_code=303)


@router.get("/registrar", response_class=HTMLResponse)
def tela_registrar(request: Request):
    primeiro = not crud.existe_algum_usuario()
    return templates.TemplateResponse(
        "registrar.html", {"request": request, "usuario": None, "erro": None, "primeiro_usuario": primeiro}
    )


@router.post("/registrar")
def fazer_registro(
    request: Request,
    nome: str = Form(...),
    email: str = Form(...),
    senha: str = Form(...),
    telefone_whatsapp: str = Form(""),
):
    if crud.buscar_usuario_por_email(email):
        primeiro = not crud.existe_algum_usuario()
        return templates.TemplateResponse(
            "registrar.html",
            {"request": request, "usuario": None, "erro": "este email já está cadastrado", "primeiro_usuario": primeiro},
        )
    # O primeiro cadastro do sistema vira master automaticamente — é o contato
    # de acesso geral mencionado na implantação.
    is_master = not crud.existe_algum_usuario()
    usuario = crud.criar_usuario(email, senha, nome, telefone_whatsapp or None, is_master)
    request.session["usuario_id"] = usuario["id"]
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
