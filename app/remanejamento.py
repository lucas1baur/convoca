"""
Ordem de remanejamento — PARAMETRIZÁVEL e EDITÁVEL.

Como é legislação e pode mudar, a ordem NÃO é fixada em código: fica na tabela
`remanejamento_regra`, e pode ser editada pela tela de Regras. Semeamos uma vez
a ordem padrão (DOCX §27); a partir daí o usuário altera quando a legislação mudar.
"""

from .db import get_conn, MODALIDADES

# Ordem padrão (DOCX §27). Serve só para a primeira carga; depois é editável.
ORDEM_PADRAO = {
    "LB_PPI": ["LB_Q", "LB_PcD", "LB_EP", "LI_PPI", "LI_Q", "LI_PcD", "LI_EP", "AC"],
    "LB_Q":   ["LB_PPI", "LB_PcD", "LB_EP", "LI_PPI", "LI_Q", "LI_PcD", "LI_EP", "AC"],
    "LB_PcD": ["LB_PPI", "LB_Q", "LB_EP", "LI_PPI", "LI_Q", "LI_PcD", "LI_EP", "AC"],
    "LB_EP":  ["LB_PPI", "LB_Q", "LB_PcD", "LI_PPI", "LI_Q", "LI_PcD", "LI_EP", "AC"],
    "LI_PPI": ["LB_PPI", "LB_Q", "LB_PcD", "LB_EP", "LI_Q", "LI_PcD", "LI_EP", "AC"],
    "LI_Q":   ["LB_PPI", "LB_Q", "LB_PcD", "LB_EP", "LI_PPI", "LI_PcD", "LI_EP", "AC"],
    "LI_PcD": ["LB_PPI", "LB_Q", "LB_PcD", "LB_EP", "LI_PPI", "LI_Q", "LI_EP", "AC"],
    "LI_EP":  ["LB_PPI", "LB_Q", "LB_PcD", "LB_EP", "LI_PPI", "LI_Q", "LI_PcD", "AC"],
    # V_EFA / V_PcD: esgotou a lista própria -> vai direto p/ AC (§29).
    "V_EFA":  ["AC"],
    "V_PcD":  ["AC"],
}

VERSAO_UNICA = "vigente"


def seed_regras():
    """Semeia a ordem padrão só se a tabela estiver vazia (primeira execução)."""
    conn = get_conn()
    c = conn.cursor()
    ja = c.execute("SELECT 1 FROM remanejamento_regra LIMIT 1").fetchone()
    if not ja:
        for origem, destinos in ORDEM_PADRAO.items():
            for i, destino in enumerate(destinos, start=1):
                c.execute("""INSERT INTO remanejamento_regra
                             (versao, vigente, origem, passo, destino)
                             VALUES (?, 1, ?, ?, ?)""",
                          (VERSAO_UNICA, origem, i, destino))
        conn.commit()
    conn.close()


def get_ordem_vigente(conn):
    """Retorna {origem: [destinos em ordem]} — a ordem atual (única)."""
    rows = conn.execute("""SELECT origem, passo, destino FROM remanejamento_regra
                           WHERE vigente=1 ORDER BY origem, passo""").fetchall()
    ordem = {}
    for r in rows:
        ordem.setdefault(r["origem"], []).append(r["destino"])
    return ordem


def get_ordem_completa():
    """Retorna a ordem atual para exibição/edição na tela."""
    conn = get_conn()
    ordem = get_ordem_vigente(conn)
    conn.close()
    return ordem


def salvar_ordem(nova_ordem):
    """Substitui toda a tabela de remanejamento pela nova ordem editada.

    nova_ordem: dict {origem: [destinos em ordem]}. Cada destino deve ser uma
    modalidade válida. Entradas vazias são ignoradas. Regrava do zero, de forma
    atômica, para refletir exatamente o que o usuário definiu."""
    validas = set(MODALIDADES)
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM remanejamento_regra")
        for origem, destinos in nova_ordem.items():
            if origem not in validas:
                continue
            passo = 1
            for destino in destinos:
                if not destino:
                    continue
                destino = destino.strip()
                if destino not in validas:
                    continue
                c.execute("""INSERT INTO remanejamento_regra
                             (versao, vigente, origem, passo, destino)
                             VALUES (?, 1, ?, ?, ?)""",
                          (VERSAO_UNICA, origem, passo, destino))
                passo += 1
        conn.commit()
    finally:
        conn.close()


def modalidades_disponiveis():
    """Lista de modalidades válidas, para os seletores da tela de edição."""
    return list(MODALIDADES)
