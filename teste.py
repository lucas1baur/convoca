"""Validação v2: 1ª chamada = transcrição dos classificados; 2ª+ = motor."""
import app.db as db
if db.DB_PATH.exists():
    db.DB_PATH.unlink()
db.init_db()

from app.remanejamento import seed_regras
from app import importador, motor

seed_regras()
XLSX = "/mnt/user-data/uploads/Enfermagem_-_Subsequente.xlsx"

conn = db.get_conn()
conn.execute("INSERT INTO processo (nome) VALUES ('PS 1048/2025')")
proc_id = conn.execute("SELECT id FROM processo").fetchone()["id"]
conn.commit(); conn.close()

curso_id, n = importador.importar(XLSX, proc_id, curso_nome="Enfermagem")
print(f"Importados: {n}")

# ETAPA 0: derivar vagas + 1a chamada (transcrição)
r0 = motor.derivar_vagas_e_primeira_chamada(proc_id, curso_id)
print(f"\n1ª CHAMADA (transcrição): {len(r0['convocacoes'])} classificados convocados")
print("  Vagas por modalidade:", importador.vagas_por_modalidade(curso_id))

# HETERO (antecipada)
h = motor.convocar_hetero(proc_id)
print(f"\nHETERO: {len(h['convocados'])} PPI convocados (classif+espera)")
ppi_ids = [c["id"] for c in h["convocados"]]
for cid in ppi_ids:
    motor.registrar_hetero(cid, "HOMOLOGADO")
motor.registrar_hetero(ppi_ids[-1], "AUSENTE")   # força bloqueio
print(f"  Forçado AUSENTE em id={ppi_ids[-1]}")

# Registrar resultado da 1a chamada: alguns não comparecem -> gera vagas p/ 2a
conn = db.get_conn()
convs = conn.execute("""SELECT cv.id, cv.candidato_id, cv.modalidade_convocacao, c.nome
                        FROM convocacao cv JOIN candidato c ON c.id=cv.candidato_id
                        JOIN chamada ch ON ch.id=cv.chamada_id
                        WHERE ch.tipo='MATRICULA' AND ch.numero=1""").fetchall()
conn.close()
# homologa a maioria, mas 3 não comparecem (2 AC, 1 LB_EP) para forçar 2a chamada
nao_comp = 0
for cv in convs:
    if nao_comp < 3 and cv["modalidade_convocacao"] in ("AC","LB_EP"):
        motor.registrar_matricula(cv["id"], "NAO_COMPARECEU")
        print(f"  NÃO COMPARECEU: {cv['nome']} ({cv['modalidade_convocacao']})")
        nao_comp += 1
    else:
        motor.registrar_matricula(cv["id"], "HOMOLOGADO")

# 2a CHAMADA: agora o motor age nas vagas reabertas
print("\n=== 2ª CHAMADA (motor: fila de espera + remanejamento) ===")
r2 = motor.gerar_chamada_matricula(proc_id)
for x in r2["convocacoes"]:
    if x["candidato"]:
        flag = "" if x["modalidade_convocacao"]==x["modalidade_original"] else "  <-- REMANEJADA"
        print(f"  vaga {x['modalidade_original']} -> {x['modalidade_convocacao']}: {x['candidato']}{flag}")
    else:
        print(f"  vaga {x['modalidade_original']}: SEM ELEGÍVEL")

# rastreabilidade da 2a chamada
print("\n=== RASTREABILIDADE (2ª chamada) ===")
conn = db.get_conn()
ch2 = conn.execute("SELECT id FROM chamada WHERE tipo='MATRICULA' AND numero=2").fetchone()["id"]
logs = conn.execute("""SELECT l.acao, l.modalidade, l.motivo, c.nome
                       FROM log_decisao l LEFT JOIN candidato c ON c.id=l.candidato_id
                       WHERE l.chamada_id=? ORDER BY l.id LIMIT 12""", (ch2,)).fetchall()
for lg in logs:
    who = f"[{lg['nome']}] " if lg["nome"] else ""
    print(f"  {lg['acao']:<18} {who}{lg['motivo']}")
conn.close()

print("\nOK v2")
