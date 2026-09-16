"""
Motor de decisão — o coração do sistema.

Implementa as regras da Especificação Funcional:
  §8-§16  heteroidentificação antecipada dos PPI (classificados + lista de espera)
  §13/§14 indeferido/ausente -> bloqueia TODAS as cotas, mantém só AC
  §19     uma única oportunidade em reserva: chamado em cota depois não vai p/ outra
  §24/§43 procurar o próximo ELEGÍVEL (10 verificações) antes de convocar
  §26-§29 remanejamento quando não há elegível (ordem parametrizável; V_EFA especial)
  §38     vaga reaberta volta pela modalidade da ÚLTIMA matrícula, não a original
  §41     toda decisão registra o porquê (log de rastreabilidade)

O motor NUNCA reclassifica: só lê a classificação original e decide elegibilidade.
"""

from .db import get_conn, MODALIDADES_PPI
from .remanejamento import get_ordem_vigente


# ----------------------------------------------------------------------------- 
# ETAPA 0a — Derivar as vagas a partir da classificação da banca (na importação)
# ----------------------------------------------------------------------------- 
def derivar_vagas(processo_id, curso_id):
    """Cria as vagas ofertadas a partir da classificação da banca: uma vaga por
    candidato CLASSIFICADO, na modalidade em que a banca o classificou. Guarda o
    candidato como 'titular' da vaga (candidato_atual_id), mas NÃO gera chamada
    nem convoca ninguém. A convocação (1ª chamada) só acontece depois da hetero.
    A estrutura de vagas reflete a oferta da banca e não muda com a hetero."""
    conn = get_conn()
    cur = conn.cursor()
    ja = cur.execute("SELECT COUNT(*) n FROM vaga WHERE curso_id=?", (curso_id,)).fetchone()["n"]
    if ja:
        conn.close()
        return {"erro": "Vagas já foram derivadas para este curso."}

    classificados = cur.execute("""
        SELECT * FROM candidato
        WHERE curso_id=? AND UPPER(situacao_banca)='CLASSIFICADO'
        ORDER BY posicao_geral""", (curso_id,)).fetchall()

    n = 0
    for c in classificados:
        mod = c["modalidade_classificacao"]
        # titular da vaga = o classificado pela banca; status ABERTA (ainda não convocado)
        cur.execute("""INSERT INTO vaga (curso_id, modalidade_original, modalidade_atual,
                       candidato_atual_id, status) VALUES (?,?,?,?, 'ABERTA')""",
                    (curso_id, mod, mod, c["id"]))
        n += 1

    conn.commit()
    conn.close()
    return {"vagas_criadas": n}


# ----------------------------------------------------------------------------- 
# ETAPA 0b — Gerar a 1ª chamada (botão, DEPOIS da heteroidentificação)
# ----------------------------------------------------------------------------- 
def hetero_pendente(processo_id):
    """Retorna a lista de nomes de PPI convocados que ainda não têm resultado de
    hetero. Se a lista estiver vazia, a hetero está completa (ou não houve PPI)."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT c.nome FROM convocacao cv
        JOIN chamada ch ON ch.id=cv.chamada_id AND ch.tipo='HETERO'
        JOIN candidato c ON c.id=cv.candidato_id
        JOIN elegibilidade e ON e.candidato_id=c.id
        WHERE ch.processo_id=? AND e.resultado_hetero IS NULL
        ORDER BY c.nome""", (processo_id,)).fetchall()
    conn.close()
    return [r["nome"] for r in rows]


def gerar_primeira_chamada(processo_id):
    """Gera a 1ª chamada DEPOIS da heteroidentificação.

    Para cada vaga (na ordem da banca), o titular classificado pela banca é
    convocado SE for elegível na modalidade da vaga. Como a hetero já foi lançada:
      - PPI homologado (ou vaga não-PPI): convoca o titular, como a banca definiu;
      - PPI indeferido/ausente: a vaga é REMANEJADA para o próximo elegível da
        cadeia; o titular indeferido, com cotas já bloqueadas pela hetero, segue
        concorrendo apenas em AC (pela posição dele em AC, nas vagas de AC).

    Bloqueia se houver PPI sem resultado de hetero."""
    conn = get_conn()
    cur = conn.cursor()

    # trava: 1ª chamada só com hetero completa
    pend = cur.execute("""
        SELECT COUNT(*) n FROM convocacao cv
        JOIN chamada ch ON ch.id=cv.chamada_id AND ch.tipo='HETERO'
        JOIN elegibilidade e ON e.candidato_id=cv.candidato_id
        WHERE ch.processo_id=? AND e.resultado_hetero IS NULL""", (processo_id,)).fetchone()["n"]
    if pend:
        conn.close()
        raise ValueError(f"Heteroidentificação incompleta: {pend} candidato(s) PPI "
                         f"ainda sem resultado. Lance todos antes de gerar a 1ª chamada.")

    ja = cur.execute("""SELECT COUNT(*) n FROM chamada
                        WHERE processo_id=? AND tipo='MATRICULA'""", (processo_id,)).fetchone()["n"]
    if ja:
        conn.close()
        raise ValueError("A 1ª chamada já foi gerada.")

    ordem = get_ordem_vigente(conn)
    cur.execute("INSERT INTO chamada (processo_id, numero, tipo) VALUES (?,1,'MATRICULA')",
                (processo_id,))
    chamada_id = cur.lastrowid

    # vagas na ordem da banca (id crescente = ordem de classificação)
    vagas = cur.execute("""
        SELECT v.* FROM vaga v JOIN curso cu ON cu.id=v.curso_id
        WHERE cu.processo_id=? ORDER BY v.curso_id, v.id""", (processo_id,)).fetchall()

    ja_convocados = set()
    resultado = []

    for v in vagas:
        curso_id = v["curso_id"]
        titular_id = v["candidato_atual_id"]
        mod_vaga = v["modalidade_original"]
        eh_ppi = mod_vaga in MODALIDADES_PPI

        escolhido = None
        mod_convocacao = None

        if not eh_ppi:
            # Vaga NÃO-PPI: a classificação da banca é soberana. A hetero não afeta.
            # Convoca o titular exatamente como a banca definiu, sem remanejar.
            if titular_id is not None and titular_id not in ja_convocados:
                tit = cur.execute("SELECT * FROM candidato WHERE id=?", (titular_id,)).fetchone()
                escolhido = tit
                mod_convocacao = mod_vaga
        else:
            # Vaga PPI: aqui, e só aqui, a heteroidentificação pesa.
            titular_ok = False
            if titular_id is not None and titular_id not in ja_convocados:
                tit = cur.execute("""SELECT c.*, e.* FROM candidato c
                                     JOIN elegibilidade e ON e.candidato_id=c.id
                                     WHERE c.id=?""", (titular_id,)).fetchone()
                ok, motivo = elegivel_para(tit, tit, mod_vaga)
                titular_ok = ok
                if not ok:
                    _log(conn, processo_id, chamada_id, titular_id, v["id"], mod_vaga,
                         "PULADO",
                         f"Titular PPI classificado em {mod_vaga} não homologado na "
                         f"heteroidentificação: {motivo}")
            if titular_ok:
                escolhido = tit
                mod_convocacao = mod_vaga
            else:
                # remaneja a vaga PPI pela cadeia (inclui a própria modalidade p/ o próximo da fila)
                cadeia = [mod_vaga] + ordem.get(mod_vaga, [])
                for i, mod in enumerate(cadeia):
                    cand = _proximo_elegivel(conn, curso_id, mod, processo_id, chamada_id,
                                             v["id"], ja_convocados,
                                             ignorar_classificados=True)
                    if cand:
                        escolhido = cand; mod_convocacao = mod
                        if mod != mod_vaga:
                            _log(conn, processo_id, chamada_id, None, v["id"], mod_vaga,
                                 "REMANEJADA",
                                 f"Titular PPI não homologado; vaga remanejada de {mod_vaga} para {mod}.")
                        break

        if escolhido:
            ja_convocados.add(escolhido["id"])
            reserva = (mod_convocacao != "AC")
            cur.execute("""INSERT INTO convocacao
                (chamada_id, candidato_id, vaga_id, modalidade_convocacao)
                VALUES (?,?,?,?)""", (chamada_id, escolhido["id"], v["id"], mod_convocacao))
            cur.execute("""UPDATE elegibilidade SET situacao_atual='CONVOCADO',
                           ja_chamado_reserva = CASE WHEN ? THEN 1 ELSE ja_chamado_reserva END
                           WHERE candidato_id=?""", (reserva, escolhido["id"]))
            cur.execute("UPDATE vaga SET modalidade_atual=?, candidato_atual_id=? WHERE id=?",
                        (mod_convocacao, escolhido["id"], v["id"]))
            origem_txt = ("classificado pela banca" if escolhido["id"] == titular_id
                          else "convocado por remanejamento")
            _log(conn, processo_id, chamada_id, escolhido["id"], v["id"], mod_convocacao,
                 "CONVOCADO",
                 f"1ª chamada: {origem_txt} em {mod_convocacao} "
                 f"(posição {_pos(escolhido, mod_convocacao)}).")
            resultado.append({"vaga_id": v["id"], "candidato": escolhido["nome"],
                              "inscricao": escolhido["inscricao"],
                              "modalidade_original": mod_vaga,
                              "modalidade_convocacao": mod_convocacao})
        else:
            cur.execute("UPDATE vaga SET candidato_atual_id=NULL WHERE id=?", (v["id"],))
            _log(conn, processo_id, chamada_id, None, v["id"], mod_vaga,
                 "VAGA_NAO_PREENCHIDA",
                 f"Sem elegível a partir de {mod_vaga} após a heteroidentificação.")
            resultado.append({"vaga_id": v["id"], "candidato": None,
                              "modalidade_original": mod_vaga, "modalidade_convocacao": None})

    conn.commit()
    conn.close()
    return {"chamada_id": chamada_id, "numero": 1, "convocacoes": resultado}


# ----------------------------------------------------------------------------- 
# Utilidades de log
# ----------------------------------------------------------------------------- 
def _log(conn, processo_id, chamada_id, cand_id, vaga_id, modalidade, acao, motivo):
    conn.execute("""INSERT INTO log_decisao
        (processo_id, chamada_id, candidato_id, vaga_id, modalidade, acao, motivo)
        VALUES (?,?,?,?,?,?,?)""",
        (processo_id, chamada_id, cand_id, vaga_id, modalidade, acao, motivo))


def _pos(cand, modalidade):
    """Posição do candidato na lista da modalidade (ou None)."""
    return cand[f"pos_{modalidade.lower()}"]


# ----------------------------------------------------------------------------- 
# ETAPA 1 — Heteroidentificação (§8-§16)
# ----------------------------------------------------------------------------- 
def convocar_hetero(processo_id):
    """Convoca para heteroidentificação TODOS os PPI (classificados + espera),
    antecipadamente (§9). Retorna a lista de convocados."""
    conn = get_conn()
    cur = conn.cursor()
    n_ch = _proximo_numero(conn, processo_id, "HETERO")
    cur.execute("INSERT INTO chamada (processo_id, numero, tipo) VALUES (?,?,'HETERO')",
                (processo_id, n_ch))
    chamada_id = cur.lastrowid

    # PPI = tem posição em LB_PPI ou LI_PPI
    cond = " OR ".join(f"c.pos_{m.lower()} IS NOT NULL" for m in MODALIDADES_PPI)
    cands = cur.execute(f"""
        SELECT c.* FROM candidato c
        JOIN curso cu ON cu.id=c.curso_id
        WHERE cu.processo_id=? AND ({cond})
        ORDER BY c.curso_id, c.posicao_geral""", (processo_id,)).fetchall()

    convocados = []
    for c in cands:
        cur.execute("""INSERT INTO convocacao
            (chamada_id, candidato_id, modalidade_convocacao) VALUES (?,?, 'PPI')""",
            (chamada_id, c["id"]))
        cur.execute("UPDATE elegibilidade SET convocado_hetero=1 WHERE candidato_id=?",
                    (c["id"],))
        _log(conn, processo_id, chamada_id, c["id"], None, "PPI", "CONVOCADO_HETERO",
             "Convocado antecipadamente para heteroidentificação (PPI, §9).")
        convocados.append(dict(c))

    conn.commit()
    conn.close()
    return {"chamada_id": chamada_id, "numero": n_ch, "convocados": convocados}


def registrar_hetero(candidato_id, resultado):
    """Registra HOMOLOGADO | INDEFERIDO | AUSENTE e aplica efeitos (§12-§14)."""
    resultado = resultado.upper()
    conn = get_conn()
    cur = conn.cursor()
    cand = cur.execute("SELECT * FROM candidato WHERE id=?", (candidato_id,)).fetchone()
    proc = cur.execute("SELECT cu.processo_id p FROM candidato c JOIN curso cu ON cu.id=c.curso_id WHERE c.id=?",
                       (candidato_id,)).fetchone()["p"]

    cur.execute("UPDATE elegibilidade SET resultado_hetero=? WHERE candidato_id=?",
                (resultado, candidato_id))

    if resultado == "HOMOLOGADO":
        _log(conn, proc, None, candidato_id, None, "PPI", "HETERO_HOMOLOGADO",
             "Condição PPI homologada. Segue aguardando as listas em que é elegível (§12).")
    elif resultado in ("INDEFERIDO", "AUSENTE"):
        # §13/§14: bloqueia todas as cotas; mantém só AC se houver classificação em AC.
        so_ac = cand["pos_ac"] is not None
        cur.execute("""UPDATE elegibilidade
                       SET cotas_bloqueadas=1,
                           situacao_atual=CASE WHEN ? THEN 'SO_AC' ELSE 'INDEFERIDO' END
                       WHERE candidato_id=?""", (so_ac, candidato_id))
        motivo = ("Heteroidentificação " + resultado.lower() +
                  ": todas as cotas bloqueadas; " +
                  ("permanece somente na AC (§14)." if so_ac
                   else "sem posição em AC, sai do processo (§13)."))
        _log(conn, proc, None, candidato_id, None, "PPI", f"HETERO_{resultado}", motivo)
    else:
        conn.rollback(); conn.close()
        raise ValueError("Resultado inválido.")

    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------- 
# Elegibilidade de um candidato para uma modalidade (as 10 verificações §43)
# ----------------------------------------------------------------------------- 
def elegivel_para(cand, eleg, modalidade):
    """Retorna (True, '') se o candidato pode ser convocado NESTA modalidade,
    ou (False, motivo) explicando por que não. Ordem = §43."""
    # (3) tem posição nessa lista?
    if _pos(cand, modalidade) is None:
        return False, f"Não figura na lista {modalidade}."
    # (8) já matriculado?
    if eleg["matriculado"]:
        return False, "Já está matriculado."
    # já convocado e aguardando resultado (não pode ser convocado de novo)
    if eleg["situacao_atual"] == "CONVOCADO":
        return False, "Já convocado; aguardando resultado da matrícula."
    # (9) situação impeditiva? ELIMINADO = perdeu na AC (ou em cota sem posição AC).
    if eleg["situacao_atual"] in ("ELIMINADO", "DESISTENTE", "INDEFERIDO",
                                   "MATRICULADO", "PERDEU_VAGA"):
        return False, f"Situação impeditiva: {eleg['situacao_atual']}."
    # cotas bloqueadas (§13/§14/§19) — só AC passa
    if modalidade != "AC" and eleg["cotas_bloqueadas"]:
        return False, "Cotas bloqueadas; só pode ser convocado em AC."
    # (5)(6) heteroidentificação: se a modalidade é PPI, precisa estar homologado
    if modalidade in MODALIDADES_PPI:
        rh = eleg["resultado_hetero"]
        if rh is None:
            return False, "Heteroidentificação ainda não realizada para modalidade PPI."
        if rh != "HOMOLOGADO":
            return False, f"Heteroidentificação {rh.lower()}: inelegível em PPI."
    # (7) já chamado em reserva e esta é outra reserva? (§19)
    if modalidade != "AC" and eleg["ja_chamado_reserva"]:
        return False, "Já foi chamado por uma reserva; não pode outra reserva (§19)."
    return True, ""


# ----------------------------------------------------------------------------- 
# Buscar próximo elegível numa modalidade, seguindo a classificação (§24)
# ----------------------------------------------------------------------------- 
def _proximo_elegivel(conn, curso_id, modalidade, processo_id, chamada_id, vaga_id,
                      ja_convocados, ignorar_classificados=False):
    """Percorre a lista da modalidade por ordem de posição; devolve o 1º elegível.
    Registra no log cada candidato PULADO e o porquê.

    ignorar_classificados=True (usado só na 1ª chamada): pula quem a banca já
    CLASSIFICOU (esses têm vaga garantida e não podem ser 'roubados' por um
    remanejamento). Nas chamadas seguintes esse filtro é False, porque quem não
    matriculou já liberou a vaga e volta a concorrer pela fila normalmente."""
    cands = conn.execute(f"""
        SELECT c.*, e.* FROM candidato c
        JOIN elegibilidade e ON e.candidato_id=c.id
        WHERE c.curso_id=? AND c.pos_{modalidade.lower()} IS NOT NULL
        ORDER BY c.pos_{modalidade.lower()}""", (curso_id,)).fetchall()

    for c in cands:
        if c["id"] in ja_convocados:
            continue
        # na 1ª chamada, quem a banca classificou fica na vaga dele: não entra em remanejamento
        if ignorar_classificados and (c["situacao_banca"] or "").upper() == "CLASSIFICADO":
            continue
        ok, motivo = elegivel_para(c, c, modalidade)
        if ok:
            return c
        _log(conn, processo_id, chamada_id, c["id"], vaga_id, modalidade,
             "PULADO", f"Posição {_pos(c, modalidade)} em {modalidade}: {motivo}")
    return None


# ----------------------------------------------------------------------------- 
# ETAPA 2 — Gerar uma chamada de matrícula (§17, §30, §43)
# ----------------------------------------------------------------------------- 
def gerar_chamada_matricula(processo_id):
    """Para cada vaga ABERTA/LIBERADA, encontra o próximo elegível na modalidade
    atual da vaga; se não houver, remaneja pela ordem vigente. Registra tudo."""
    conn = get_conn()
    cur = conn.cursor()
    ordem = get_ordem_vigente(conn)

    n_ch = _proximo_numero(conn, processo_id, "MATRICULA")
    cur.execute("INSERT INTO chamada (processo_id, numero, tipo) VALUES (?,?,'MATRICULA')",
                (processo_id, n_ch))
    chamada_id = cur.lastrowid

    vagas = cur.execute("""
        SELECT v.* FROM vaga v JOIN curso cu ON cu.id=v.curso_id
        WHERE cu.processo_id=? AND v.status IN ('ABERTA','LIBERADA')
        ORDER BY v.curso_id, v.id""", (processo_id,)).fetchall()

    ja_convocados = set()  # evita convocar o mesmo candidato para 2 vagas na mesma chamada
    resultado = []

    for v in vagas:
        curso_id = v["curso_id"]
        mod_inicial = v["modalidade_atual"]
        cadeia = [mod_inicial] + ordem.get(mod_inicial, [])
        escolhido = None
        mod_convocacao = None

        for i, mod in enumerate(cadeia):
            cand = _proximo_elegivel(conn, curso_id, mod, processo_id, chamada_id,
                                     v["id"], ja_convocados)
            if cand:
                escolhido = cand
                mod_convocacao = mod
                if i > 0:
                    _log(conn, processo_id, chamada_id, None, v["id"], mod_inicial,
                         "REMANEJADA",
                         f"Sem elegível em {mod_inicial}; remanejada para {mod} (passo {i}).")
                break
            elif i < len(cadeia) - 1:
                _log(conn, processo_id, chamada_id, None, v["id"], mod,
                     "SEM_ELEGIVEL", f"Nenhum elegível em {mod}; tenta próxima modalidade.")

        if escolhido:
            ja_convocados.add(escolhido["id"])
            reserva = (mod_convocacao != "AC")
            cur.execute("""INSERT INTO convocacao
                (chamada_id, candidato_id, vaga_id, modalidade_convocacao)
                VALUES (?,?,?,?)""",
                (chamada_id, escolhido["id"], v["id"], mod_convocacao))
            cur.execute("""UPDATE elegibilidade SET situacao_atual='CONVOCADO',
                           ja_chamado_reserva = CASE WHEN ? THEN 1 ELSE ja_chamado_reserva END
                           WHERE candidato_id=?""", (reserva, escolhido["id"]))
            cur.execute("UPDATE vaga SET modalidade_atual=?, candidato_atual_id=? WHERE id=?",
                        (mod_convocacao, escolhido["id"], v["id"]))
            _log(conn, processo_id, chamada_id, escolhido["id"], v["id"], mod_convocacao,
                 "CONVOCADO",
                 f"Convocado como {mod_convocacao} (posição {_pos(escolhido, mod_convocacao)}). "
                 + ("Reserva de vaga (§19)." if reserva else "Ampla Concorrência (§18)."))
            resultado.append({
                "vaga_id": v["id"], "candidato": escolhido["nome"],
                "inscricao": escolhido["inscricao"],
                "modalidade_original": v["modalidade_original"],
                "modalidade_convocacao": mod_convocacao,
            })
        else:
            _log(conn, processo_id, chamada_id, None, v["id"], mod_inicial,
                 "VAGA_NAO_PREENCHIDA",
                 f"Esgotada a cadeia de remanejamento a partir de {mod_inicial}: sem elegível.")
            resultado.append({
                "vaga_id": v["id"], "candidato": None,
                "modalidade_original": v["modalidade_original"],
                "modalidade_convocacao": None,
            })

    conn.commit()
    conn.close()
    return {"chamada_id": chamada_id, "numero": n_ch, "convocacoes": resultado}


# ----------------------------------------------------------------------------- 
# ETAPA 3 — Registrar resultado da matrícula (§20-§22)
# ----------------------------------------------------------------------------- 
def registrar_matricula(convocacao_id, resultado):
    """resultado: HOMOLOGADO | INDEFERIDO | NAO_COMPARECEU | NAO_REALIZOU | DESISTENTE."""
    resultado = resultado.upper()
    conn = get_conn()
    cur = conn.cursor()
    conv = cur.execute("SELECT * FROM convocacao WHERE id=?", (convocacao_id,)).fetchone()
    if not conv:
        conn.close(); raise ValueError("Convocação não encontrada.")
    proc = cur.execute("""SELECT cu.processo_id p FROM candidato c
                          JOIN curso cu ON cu.id=c.curso_id WHERE c.id=?""",
                       (conv["candidato_id"],)).fetchone()["p"]
    cand_id = conv["candidato_id"]
    vaga_id = conv["vaga_id"]
    mod = conv["modalidade_convocacao"]

    # snapshot do estado ANTES de aplicar efeitos (para permitir correção depois)
    eleg_antes = cur.execute("SELECT situacao_atual FROM elegibilidade WHERE candidato_id=?",
                             (cand_id,)).fetchone()
    vaga_antes = cur.execute("SELECT status, modalidade_atual, modalidade_ultima_matricula FROM vaga WHERE id=?",
                             (vaga_id,)).fetchone()
    cur.execute("""UPDATE convocacao SET snap_situacao=?, snap_vaga_status=?,
                   snap_vaga_mod_atual=?, snap_vaga_mod_ultima=? WHERE id=?""",
                (eleg_antes["situacao_atual"], vaga_antes["status"],
                 vaga_antes["modalidade_atual"], vaga_antes["modalidade_ultima_matricula"],
                 convocacao_id))

    cur.execute("UPDATE convocacao SET resultado=? WHERE id=?", (resultado, convocacao_id))

    if resultado == "HOMOLOGADO":
        # §20: ocupa a vaga; modalidade vigente da vaga = modalidade da matrícula (§35).
        cur.execute("""UPDATE elegibilidade SET matriculado=1, situacao_atual='MATRICULADO',
                       modalidade_matricula=? WHERE candidato_id=?""", (mod, cand_id))
        cur.execute("""UPDATE vaga SET preenchida=1, status='PREENCHIDA',
                       modalidade_atual=?, modalidade_ultima_matricula=?,
                       candidato_atual_id=? WHERE id=?""",
                    (mod, mod, cand_id, vaga_id))
        _log(conn, proc, conv["chamada_id"], cand_id, vaga_id, mod, "MATRICULADO",
             f"Matrícula homologada em {mod}. Vaga passa a vigorar como {mod} (§35).")
    else:
        # QUALQUER falha (não compareceu, indeferido, não realizou, desistente) tem
        # o mesmo efeito; o efeito depende só da MODALIDADE da convocação:
        #   - falha em COTA  -> bloqueia todas as cotas; permanece na AC se tiver
        #                       posição em AC (§19/§21). Não é o fim do processo.
        #   - falha em AC    -> terminal: perde a vaga e SAI do processo.
        # Falhas terminais/relevantes: NAO_COMPARECEU, NAO_REALIZOU, DESISTENTE, INDEFERIDO.
        cur.execute("UPDATE vaga SET candidato_atual_id=NULL, status='ABERTA' WHERE id=?",
                    (vaga_id,))
        rot = {"NAO_COMPARECEU": "não compareceu",
               "NAO_REALIZOU": "não realizou a matrícula",
               "DESISTENTE": "desistiu",
               "INDEFERIDO": "foi indeferido"}.get(resultado, resultado.lower())

        if mod == "AC":
            # Falhou na Ampla Concorrência: eliminado de todo o processo.
            eleg = cur.execute("SELECT cotas_bloqueadas FROM elegibilidade WHERE candidato_id=?",
                               (cand_id,)).fetchone()
            ja_bloqueado = eleg["cotas_bloqueadas"] == 1
            cur.execute("""UPDATE elegibilidade SET situacao_atual='ELIMINADO',
                           cotas_bloqueadas=1 WHERE candidato_id=?""", (cand_id,))
            if not ja_bloqueado:
                cur.execute("UPDATE convocacao SET bloqueou_cotas=1 WHERE id=?", (convocacao_id,))
            _log(conn, proc, conv["chamada_id"], cand_id, vaga_id, mod, "ELIMINADO",
                 f"Convocado em AC e {rot}: perde a vaga e sai do processo. "
                 f"Não será convocado em nenhuma outra lista.")
        else:
            # Falhou em cota: bloqueia todas as cotas; segue só na AC se tiver posição.
            cand = cur.execute("SELECT * FROM candidato WHERE id=?", (cand_id,)).fetchone()
            eleg = cur.execute("SELECT cotas_bloqueadas FROM elegibilidade WHERE candidato_id=?",
                               (cand_id,)).fetchone()
            ja_bloqueado = eleg["cotas_bloqueadas"] == 1
            so_ac = cand["pos_ac"] is not None
            cur.execute("""UPDATE elegibilidade SET cotas_bloqueadas=1,
                           situacao_atual=CASE WHEN ? THEN 'SO_AC' ELSE 'ELIMINADO' END
                           WHERE candidato_id=?""", (so_ac, cand_id))
            if not ja_bloqueado:
                cur.execute("UPDATE convocacao SET bloqueou_cotas=1 WHERE id=?", (convocacao_id,))
            _log(conn, proc, conv["chamada_id"], cand_id, vaga_id, mod, "FALHA_COTA",
                 f"Convocado em {mod} (reserva) e {rot}: todas as cotas bloqueadas; "
                 + ("permanece somente na AC (§21)." if so_ac
                    else "sem posição em AC, sai do processo."))

    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------- 
# CORREÇÃO — desfazer o resultado de uma convocação, revertendo efeitos (cascata)
# ----------------------------------------------------------------------------- 
def pode_corrigir(conn, convocacao_id):
    """Só permite corrigir se a convocação pertence à ÚLTIMA chamada de matrícula
    do processo e nenhuma chamada posterior foi gerada. Retorna (bool, motivo)."""
    conv = conn.execute("SELECT * FROM convocacao WHERE id=?", (convocacao_id,)).fetchone()
    if not conv:
        return False, "Convocação não encontrada."
    if conv["resultado"] is None:
        return False, "Esta convocação ainda não tem resultado registrado."
    ch = conn.execute("SELECT * FROM chamada WHERE id=?", (conv["chamada_id"],)).fetchone()
    ultima = conn.execute("""SELECT MAX(numero) n FROM chamada
                             WHERE processo_id=? AND tipo='MATRICULA'""",
                          (ch["processo_id"],)).fetchone()["n"]
    if ch["numero"] != ultima:
        return False, (f"Só é possível corrigir a chamada mais recente "
                       f"({ultima}ª). Esta é a {ch['numero']}ª e já foi selada por "
                       f"chamadas posteriores.")
    return True, ""


def corrigir_convocacao(convocacao_id):
    """Desfaz o resultado registrado, revertendo TODOS os efeitos em cascata,
    e devolve a convocação para 'Aguardando'. Preserva o log (registra a correção)."""
    conn = get_conn()
    cur = conn.cursor()

    ok, motivo = pode_corrigir(conn, convocacao_id)
    if not ok:
        conn.close()
        raise ValueError(motivo)

    conv = cur.execute("SELECT * FROM convocacao WHERE id=?", (convocacao_id,)).fetchone()
    proc = cur.execute("""SELECT cu.processo_id p FROM candidato c
                          JOIN curso cu ON cu.id=c.curso_id WHERE c.id=?""",
                       (conv["candidato_id"],)).fetchone()["p"]
    cand_id = conv["candidato_id"]
    vaga_id = conv["vaga_id"]
    resultado_errado = conv["resultado"]

    # 1) Reverter estado do CANDIDATO
    #    - desfaz matrícula (se homologou)
    #    - desfaz bloqueio de cotas SE foi esta convocação que bloqueou
    cur.execute("""UPDATE elegibilidade
                   SET matriculado=0, modalidade_matricula=NULL,
                       situacao_atual=?
                   WHERE candidato_id=?""",
                (conv["snap_situacao"] or "CONVOCADO", cand_id))
    if conv["bloqueou_cotas"]:
        cur.execute("UPDATE elegibilidade SET cotas_bloqueadas=0 WHERE candidato_id=?",
                    (cand_id,))

    # 2) Reverter estado da VAGA ao snapshot (antes do resultado)
    cur.execute("""UPDATE vaga SET status=?, modalidade_atual=?,
                   modalidade_ultima_matricula=?, candidato_atual_id=?, preenchida=?
                   WHERE id=?""",
                (conv["snap_vaga_status"] or "ABERTA",
                 conv["snap_vaga_mod_atual"] or conv["modalidade_convocacao"],
                 conv["snap_vaga_mod_ultima"],
                 cand_id,
                 1 if (conv["snap_vaga_status"] == "PREENCHIDA") else 0,
                 vaga_id))

    # 3) Limpar o resultado e os snapshots
    cur.execute("""UPDATE convocacao SET resultado=NULL, bloqueou_cotas=0,
                   snap_situacao=NULL, snap_vaga_status=NULL,
                   snap_vaga_mod_atual=NULL, snap_vaga_mod_ultima=NULL
                   WHERE id=?""", (convocacao_id,))

    # 4) Rastreabilidade: registra a correção (não apaga o histórico)
    _log(conn, proc, conv["chamada_id"], cand_id, vaga_id, conv["modalidade_convocacao"],
         "CORRECAO",
         f"Resultado '{resultado_errado}' desfeito por correção manual. "
         f"Estado revertido; convocação volta a AGUARDANDO.")

    conn.commit()
    conn.close()
    return {"ok": True, "resultado_desfeito": resultado_errado}


# ----------------------------------------------------------------------------- 
# ETAPA 4 — Vaga liberada após início das aulas (§37/§38)
# ----------------------------------------------------------------------------- 
def liberar_vaga_pos_aulas(vaga_id, motivo="Não compareceu 10 dias úteis letivos (§37)."):
    """Vaga preenchida volta ao fluxo. Modalidade = a da ÚLTIMA matrícula (§38),
    não a original."""
    conn = get_conn()
    cur = conn.cursor()
    v = cur.execute("SELECT * FROM vaga WHERE id=?", (vaga_id,)).fetchone()
    proc = cur.execute("SELECT processo_id p FROM curso WHERE id=?", (v["curso_id"],)).fetchone()["p"]
    mod = v["modalidade_ultima_matricula"] or v["modalidade_atual"]

    if v["candidato_atual_id"]:
        cur.execute("UPDATE elegibilidade SET situacao_atual='PERDEU_VAGA', matriculado=0 WHERE candidato_id=?",
                    (v["candidato_atual_id"],))
    cur.execute("""UPDATE vaga SET status='LIBERADA', preenchida=0,
                   candidato_atual_id=NULL, modalidade_atual=? WHERE id=?""", (mod, vaga_id))
    _log(conn, proc, None, v["candidato_atual_id"], vaga_id, mod, "VAGA_LIBERADA",
         f"{motivo} Reabre pela modalidade da última matrícula: {mod} (§38).")
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------- 
# EXCLUSÃO DE CHAMADA — apaga a última chamada e reverte tudo que ela causou
# ----------------------------------------------------------------------------- 
def excluir_chamada(chamada_id):
    """Exclui uma chamada de matrícula inteira, revertendo todos os seus efeitos.

    Regras de segurança:
      - só a ÚLTIMA chamada de matrícula do processo pode ser excluída;
      - a 1ª chamada NÃO pode ser excluída (é a distribuição oficial da banca;
        para refazê-la, reimporte a planilha num processo novo).

    Reverte, para cada convocação da chamada (em ordem inversa à criação):
      - se teve resultado registrado, desfaz os efeitos usando o snapshot
        (matrícula, eliminação, bloqueio de cotas que ela causou, estado da vaga);
      - se ficou só convocada (sem resultado), reverte a marca de CONVOCADO e
        devolve a vaga ao estado aberto.
    Depois apaga as convocações, os logs e a própria chamada."""
    conn = get_conn()
    cur = conn.cursor()

    ch = cur.execute("SELECT * FROM chamada WHERE id=?", (chamada_id,)).fetchone()
    if not ch:
        conn.close(); raise ValueError("Chamada não encontrada.")
    if ch["tipo"] != "MATRICULA":
        conn.close(); raise ValueError("Só chamadas de matrícula podem ser excluídas.")

    proc = ch["processo_id"]
    ultima = cur.execute("""SELECT MAX(numero) n FROM chamada
                            WHERE processo_id=? AND tipo='MATRICULA'""",
                         (proc,)).fetchone()["n"]
    if ch["numero"] != ultima:
        conn.close()
        raise ValueError(f"Só é possível excluir a última chamada ({ultima}ª). "
                         f"Esta é a {ch['numero']}ª.")
    if ch["numero"] == 1:
        conn.close()
        raise ValueError("A 1ª chamada não pode ser excluída (é a distribuição "
                         "oficial da banca). Para recomeçar, reimporte a planilha.")

    # convocações desta chamada, em ordem inversa à criação
    convs = cur.execute("""SELECT * FROM convocacao WHERE chamada_id=?
                           ORDER BY id DESC""", (chamada_id,)).fetchall()

    for conv in convs:
        cand_id = conv["candidato_id"]
        vaga_id = conv["vaga_id"]

        if conv["resultado"] is not None:
            # tinha resultado: reverte pelos snapshots (igual à correção).
            # Se o snapshot era CONVOCADO, foi esta chamada (que está sendo apagada)
            # que o convocou; então ele volta a AGUARDANDO, não a CONVOCADO.
            situacao_volta = conv["snap_situacao"] or "AGUARDANDO"
            if situacao_volta == "CONVOCADO":
                situacao_volta = "AGUARDANDO"
            cur.execute("""UPDATE elegibilidade
                           SET matriculado=0, modalidade_matricula=NULL,
                               situacao_atual=?
                           WHERE candidato_id=?""",
                        (situacao_volta, cand_id))
            if conv["bloqueou_cotas"]:
                cur.execute("UPDATE elegibilidade SET cotas_bloqueadas=0 WHERE candidato_id=?",
                            (cand_id,))
            if vaga_id is not None:
                cur.execute("""UPDATE vaga SET status=?, modalidade_atual=?,
                               modalidade_ultima_matricula=?, candidato_atual_id=?,
                               preenchida=? WHERE id=?""",
                            (conv["snap_vaga_status"] or "ABERTA",
                             conv["snap_vaga_mod_atual"] or conv["modalidade_convocacao"],
                             conv["snap_vaga_mod_ultima"],
                             None,
                             1 if (conv["snap_vaga_status"] == "PREENCHIDA") else 0,
                             vaga_id))
        else:
            # só convocada, sem resultado: candidato volta a aguardar; vaga reabre.
            cur.execute("""UPDATE elegibilidade SET situacao_atual='AGUARDANDO'
                           WHERE candidato_id=? AND situacao_atual='CONVOCADO'""",
                        (cand_id,))
            if vaga_id is not None:
                cur.execute("""UPDATE vaga SET candidato_atual_id=NULL, status='ABERTA'
                               WHERE id=? AND candidato_atual_id=?""", (vaga_id, cand_id))

    # apaga convocações, logs e a chamada
    cur.execute("DELETE FROM convocacao WHERE chamada_id=?", (chamada_id,))
    cur.execute("DELETE FROM log_decisao WHERE chamada_id=?", (chamada_id,))
    cur.execute("DELETE FROM chamada WHERE id=?", (chamada_id,))

    conn.commit()
    conn.close()
    return {"ok": True, "numero_excluido": ch["numero"]}


# ----------------------------------------------------------------------------- 
def _proximo_numero(conn, processo_id, tipo):
    r = conn.execute("SELECT COALESCE(MAX(numero),0)+1 n FROM chamada WHERE processo_id=? AND tipo=?",
                     (processo_id, tipo)).fetchone()
    return r["n"]
