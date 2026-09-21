# Painel de criação/gerenciamento de agentes de IA no n8n.
#
# O servidor HTTP é biblioteca padrão do Python; as dependências reais são os
# SDKs (OpenAI, AgentOps, MCP) e o driver do Postgres — todos com wheel
# binário, então a imagem slim basta e não precisa de toolchain.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8099

WORKDIR /app

# Dependências primeiro: mudam muito menos que o código, então a camada é
# reaproveitada em todo deploy que só mexe em .py.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY index.html ./

# Sem root: o processo não escreve em disco (estado vive no Postgres), não há
# motivo pra ter permissão.
RUN useradd --system --create-home --uid 10001 painel \
    && chown -R painel:painel /app
USER painel

EXPOSE 8099

# /saude é a única rota sem auth, justamente pra isto funcionar. Devolve 503
# se o Postgres não responder, então container de pé com banco fora não passa
# por saudável.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8099')}/saude\", timeout=4).status==200 else 1)"

CMD ["python3", "servidor.py"]
