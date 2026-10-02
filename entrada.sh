#!/bin/sh
# Mesma imagem, dois processos. O painel e o worker do agente são serviços
# diferentes (ver ARQUITETURA-AGENTE.md): o painel é ferramenta interna e pode
# reiniciar à vontade; o worker atende paciente e precisa escalar e reiniciar
# sozinho, sem derrubar o painel junto.
#
# Separar por variável em vez de depender de "override de comando" na interface
# do Coolify: assim o papel fica versionado junto do resto da configuração, e
# `docker run -e PAPEL=worker` reproduz localmente o mesmo que roda em produção.
set -e

case "${PAPEL:-painel}" in
  painel)
    exec python3 servidor.py
    ;;
  worker)
    exec python3 worker_agente.py
    ;;
  *)
    echo "PAPEL inválido: '${PAPEL}'. Use 'painel' ou 'worker'." >&2
    exit 64
    ;;
esac
