"""
Camada de banco de dados (SQLite).

Princípios de modelagem (derivados da Especificação Funcional):
  - A CLASSIFICAÇÃO ORIGINAL é imutável: guardamos as posições exatamente
    como vieram da banca, e nunca as reescrevemos (§4, §45).
  - A ELEGIBILIDADE ATUAL é uma camada separada e mutável (flags por
    candidato/modalidade) — é isso que o motor lê e atualiza (§4).
  - Cada VAGA tem vida própria: modalidade_original / modalidade_atual /
    modalidade_ultima_matricula (§33, §34, §38).
  - Tudo gera rastreabilidade: cada decisão registra o porquê (§41).
"""

import sqlite3
import os
from pathlib import Path

# Caminho do banco. Localmente cai em convoca_v2.db na pasta do projeto.
# No Render, a variável de ambiente CONVOCA_DB_PATH aponta para o disco
# persistente (ex.: /var/data/convoca_v2.db), onde os dados sobrevivem a
# reinicios e novos deploys.
_env_path = os.environ.get("CONVOCA_DB_PATH")
if _env_path:
    DB_PATH = Path(_env_path)
else:
    DB_PATH = Path(__file__).resolve().parent.parent / "convoca_v2.db"

# Modalidades reconhecidas pelo sistema (siglas da planilha da banca).
MODALIDADES = [
    "AC",
    "LI_EP", "LI_PPI", "LI_Q", "LI_PcD",
    "LB_EP", "LB_PPI", "LB_Q", "LB_PcD",
    "V_EFA", "V_PcD",
]

# Modalidades consideradas PPI (sujeitas à heteroidentificação) — §8.
MODALIDADES_PPI = ["LB_PPI", "LI_PPI"]

# Modalidades de reserva de vaga (tudo que não é AC).
MODALIDADES_RESERVA = [m for m in MODALIDADES if m != "AC"]


def get_conn():
    # garante que a pasta do banco exista (importante no disco persistente do Render)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    # ---- Processo seletivo -------------------------------------------------
    c.execute("""
    CREATE TABLE IF NOT EXISTS processo (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL,
        criado_em TEXT DEFAULT (datetime('now','localtime'))
    )""")

    # ---- Curso -------------------------------------------------------------
    c.execute("""
    CREATE TABLE IF NOT EXISTS curso (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        processo_id INTEGER NOT NULL REFERENCES processo(id),
        codigo TEXT,
        nome TEXT NOT NULL,
        UNIQUE(processo_id, nome)
    )""")

    # ---- Candidato: CLASSIFICAÇÃO ORIGINAL (imutável) ----------------------
    # As colunas pos_* guardam a posição do candidato em cada lista, tal como
    # a banca entregou. NULL = candidato não figura naquela lista.
    pos_cols = ",\n        ".join(f"pos_{m.lower()} INTEGER" for m in MODALIDADES)
    c.execute(f"""
    CREATE TABLE IF NOT EXISTS candidato (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        curso_id INTEGER NOT NULL REFERENCES curso(id),
        posicao_geral INTEGER,
        inscricao TEXT NOT NULL,
        cpf TEXT,
        nome TEXT NOT NULL,
        modalidade_inscricao TEXT,
        nota REAL,
        situacao_banca TEXT,              -- CLASSIFICADO | LISTA DE ESPERA
        modalidade_classificacao TEXT,    -- modalidade em que a banca o classificou
        {pos_cols},
        UNIQUE(curso_id, inscricao)
    )""")

    # ---- Elegibilidade atual (MUTÁVEL) -------------------------------------
    # Flags que o motor consulta e altera ao longo das chamadas.
    c.execute("""
    CREATE TABLE IF NOT EXISTS elegibilidade (
        candidato_id INTEGER PRIMARY KEY REFERENCES candidato(id),
        convocado_hetero INTEGER DEFAULT 0,      -- foi chamado p/ heteroidentificação?
        resultado_hetero TEXT,                   -- HOMOLOGADO | INDEFERIDO | AUSENTE | NULL
        cotas_bloqueadas INTEGER DEFAULT 0,      -- §13/§14/§19: só pode AC
        ja_chamado_reserva INTEGER DEFAULT 0,    -- §19: já foi convocado em cota
        situacao_atual TEXT DEFAULT 'AGUARDANDO',-- AGUARDANDO|CONVOCADO|MATRICULADO|
                                                 -- ELIMINADO|SO_AC|PERDEU_VAGA
        matriculado INTEGER DEFAULT 0,
        modalidade_matricula TEXT
    )""")

    # ---- Vaga --------------------------------------------------------------
    c.execute("""
    CREATE TABLE IF NOT EXISTS vaga (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        curso_id INTEGER NOT NULL REFERENCES curso(id),
        modalidade_original TEXT NOT NULL,
        modalidade_atual TEXT NOT NULL,
        modalidade_ultima_matricula TEXT,
        preenchida INTEGER DEFAULT 0,
        candidato_atual_id INTEGER REFERENCES candidato(id),
        status TEXT DEFAULT 'ABERTA'   -- ABERTA | PREENCHIDA | LIBERADA
    )""")

    # ---- Ordem de remanejamento (PARAMETRIZÁVEL e VERSIONADA) --------------
    # Cada linha: para uma modalidade de origem, o passo N aponta a modalidade
    # de destino. Versão + vigência tornam a legislação auditável no tempo.
    c.execute("""
    CREATE TABLE IF NOT EXISTS remanejamento_regra (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        versao TEXT NOT NULL,
        vigente INTEGER DEFAULT 0,
        origem TEXT NOT NULL,
        passo INTEGER NOT NULL,
        destino TEXT NOT NULL,
        UNIQUE(versao, origem, passo)
    )""")

    # ---- Chamadas ----------------------------------------------------------
    c.execute("""
    CREATE TABLE IF NOT EXISTS chamada (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        processo_id INTEGER NOT NULL REFERENCES processo(id),
        numero INTEGER NOT NULL,
        tipo TEXT NOT NULL,            -- HETERO | MATRICULA
        criado_em TEXT DEFAULT (datetime('now','localtime'))
    )""")

    # ---- Convocações (itens de uma chamada) --------------------------------
    # Os campos snap_* guardam o estado ANTES de registrar o resultado, para
    # permitir reverter uma correção com precisão.
    c.execute("""
    CREATE TABLE IF NOT EXISTS convocacao (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chamada_id INTEGER NOT NULL REFERENCES chamada(id),
        candidato_id INTEGER NOT NULL REFERENCES candidato(id),
        vaga_id INTEGER REFERENCES vaga(id),
        modalidade_convocacao TEXT,
        resultado TEXT,                -- p/ matrícula: HOMOLOGADO|INDEFERIDO|
                                       -- NAO_COMPARECEU|NAO_REALIZOU|DESISTENTE
        bloqueou_cotas INTEGER DEFAULT 0,  -- este resultado foi o que bloqueou as cotas?
        snap_situacao TEXT,                -- situacao_atual do candidato antes
        snap_vaga_status TEXT,             -- status da vaga antes
        snap_vaga_mod_atual TEXT,          -- modalidade_atual da vaga antes
        snap_vaga_mod_ultima TEXT          -- modalidade_ultima_matricula antes
    )""")

    # ---- Log de rastreabilidade (§41) --------------------------------------
    c.execute("""
    CREATE TABLE IF NOT EXISTS log_decisao (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        processo_id INTEGER REFERENCES processo(id),
        chamada_id INTEGER REFERENCES chamada(id),
        candidato_id INTEGER REFERENCES candidato(id),
        vaga_id INTEGER REFERENCES vaga(id),
        modalidade TEXT,
        acao TEXT,          -- CONVOCADO | PULADO | REMANEJADA | VAGA_NAO_PREENCHIDA ...
        motivo TEXT,
        criado_em TEXT DEFAULT (datetime('now','localtime'))
    )""")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Banco inicializado em {DB_PATH}")
