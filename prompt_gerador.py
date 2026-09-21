"""Único ponto que toca IA no sistema: adapta o prompt-base do nicho para o
cliente novo (nome do agente, tom, particularidades), via SDK da OpenAI.
Rastreado pelo AgentOps — não decide nada sobre credenciais/sub-workflows/IDs
do Kommo, isso é tudo feito pelo cloner.py de forma determinística.
"""
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / ".pylibs"))

from config import carregar_env  # noqa: E402

import agentops  # noqa: E402
from openai import OpenAI  # noqa: E402

from templates_nicho import prompt_base_do_nicho  # noqa: E402

_inicializado = False




def _garantir_agentops():
    global _inicializado
    if _inicializado:
        return
    chave = carregar_env().get("AGENTOPS_API_KEY")
    if chave:
        agentops.init(api_key=chave)
    _inicializado = True


def gerar_prompt_cliente(nicho: str, cliente_nome: str, agente_nome: str, particularidades: str = "") -> str:
    """Chama a OpenAI para adaptar o prompt-base do nicho a este cliente específico."""
    _garantir_agentops()
    template = prompt_base_do_nicho(nicho)
    env = carregar_env()
    client = OpenAI(api_key=env["OPENAI_API_KEY"])

    instrucao = (
        "Adapte o prompt abaixo para o cliente e agente específicos. Mantenha "
        "qualquer placeholder de dado dinâmico (ex: nomes de campo entre chaves) "
        "e a estrutura geral — só personalize tom, nome do agente e o contexto "
        "do negócio. Devolva só o prompt final, sem comentário.\n\n"
        f"Nicho: {nicho}\nCliente: {cliente_nome}\nNome do agente: {agente_nome}\n"
        f"Particularidades do negócio: {particularidades or '(nenhuma informada)'}\n\n"
        f"Prompt-base do nicho:\n{template}"
    )

    resposta = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": instrucao}],
        temperature=0.4,
    )
    return resposta.choices[0].message.content.strip()


if __name__ == "__main__":
    print(gerar_prompt_cliente("clinica", "Clínica Boa Saúde", "Bia", "atende principalmente pacientes de ortodontia"))
