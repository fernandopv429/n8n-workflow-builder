from fastapi import HTTPException, Request

from .services import crud


def usuario_logado(request: Request) -> dict:
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    usuario = crud.buscar_usuario_por_id(usuario_id)
    if not usuario:
        request.session.clear()
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return usuario
