"""v1: templates de prompt por nicho, estáticos. v1.1 (opcional, flag) trocaria
por busca semântica no pgvector — a assinatura de `prompt_base_do_nicho`
continua a mesma nesse caso, só a implementação interna muda.

O workflow-origem (de onde clonar) NÃO fica mais aqui — vive na tabela
`templates_nicho` do Postgres (ver db.py), um registro por nicho, editável
sem precisar mexer em código. Isso evita ficar reclonando/recadastrando o
mesmo template toda vez — é cadastrado uma vez, usado como padrão sempre.
"""

NICHOS = {
    "clinica": "Clínica / consultório (agendamento, SDR de pacientes)",
    "advocacia": "Escritório de advocacia (qualificação de lead jurídico)",
}

# Prompt-base por nicho — ponto de partida real (extraído dos clientes já
# ativos), editar/ampliar conforme novos nichos entrarem.
PROMPTS_BASE = {
    "clinica": (
        "Você é {agente_nome}, atendente virtual de {cliente_nome}. Sua função é "
        "qualificar o lead que chega pelo WhatsApp, entender o motivo do contato, "
        "coletar nome completo e e-mail, e conduzir até a confirmação de "
        "agendamento — só avance de etapa quando o lead CONFIRMAR explicitamente "
        "que quer agendar, nunca por inferência de uma resposta genérica."
    ),
    "advocacia": (
        "Você é {agente_nome}, atendente virtual do escritório {cliente_nome}. "
        "Sua função é entender a área do direito do caso, coletar os fatos "
        "essenciais do lead e qualificar se há indício de causa antes de "
        "encaminhar para um advogado humano — nunca dê orientação jurídica você "
        "mesmo."
    ),
}

def prompt_base_do_nicho(nicho: str) -> str:
    if nicho not in PROMPTS_BASE:
        raise ValueError(f"Nicho '{nicho}' sem prompt-base cadastrado em templates_nicho.py")
    return PROMPTS_BASE[nicho]
