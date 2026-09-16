"""
Servidor local (FastAPI). Roda em http://127.0.0.1:8000
Interface web para operar o sistema de convocação.
"""
import tempfile, os, json, secrets
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from . import db, importador, motor
from .remanejamento import (seed_regras, get_ordem_completa, salvar_ordem,
                            modalidades_disponiveis)

BASE = Path(__file__).resolve().parent
app = FastAPI(title="Convoca — Controle de Chamadas do Processo Seletivo")

# ------- Autenticação (senha única) ------------------------------------------
# A senha vem da variável de ambiente CONVOCA_SENHA (nunca fica no código).
# Localmente, se não definida, cai num padrão só para desenvolvimento.
SENHA = os.environ.get("CONVOCA_SENHA", "trocar-esta-senha")
# Chave para assinar o cookie de sessão. No Render, defina CONVOCA_SECRET;
# se não houver, geramos uma aleatória a cada início.
SECRET = os.environ.get("CONVOCA_SECRET", secrets.token_hex(32))

# Caminhos liberados sem login
_LIBERADOS = ("/login", "/static", "/favicon.ico")


class AuthMiddleware(BaseHTTPMiddleware):
    """Exige login para tudo, exceto os caminhos liberados. Precisa ser adicionado
    ANTES do SessionMiddleware para que, na execução, rode DEPOIS dele (e assim
    request.session já exista)."""
    async def dispatch(self, request: Request, call_next):
        caminho = request.url.path
        if caminho.startswith(_LIBERADOS) or request.session.get("autenticado"):
            return await call_next(request)
        if caminho.startswith("/api/"):
            return JSONResponse({"detail": "Não autenticado."}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)


# ordem importa: AuthMiddleware adicionado primeiro (executa por dentro),
# SessionMiddleware por último (executa por fora, prepara request.session)
app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=60 * 60 * 12)

db.init_db()
seed_regras()

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return (BASE / "templates" / "login.html").read_text(encoding="utf-8")


@app.post("/login")
async def login_submit(request: Request, senha: str = Form(...)):
    if secrets.compare_digest(senha, SENHA):
        request.session["autenticado"] = True
        return RedirectResponse(url="/", status_code=303)
    return RedirectResponse(url="/login?erro=1", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "templates" / "index.html").read_text(encoding="utf-8")


# --------- Processos / cursos -------------------------------------------------
@app.get("/api/processos")
def processos():
    conn = db.get_conn()
    rows = conn.execute("""SELECT p.*,
        (SELECT COUNT(*) FROM curso c WHERE c.processo_id=p.id) cursos
        FROM processo p ORDER BY p.id DESC""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/processos")
def criar_processo(nome: str = Form(...)):
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO processo (nome) VALUES (?)", (nome,))
    pid = cur.lastrowid
    conn.commit(); conn.close()
    return {"id": pid, "nome": nome}


@app.post("/api/processos/{pid}/excluir")
def excluir_processo(pid: int):
    """Apaga o processo e TUDO que depende dele (cursos, candidatos, elegibilidade,
    vagas, chamadas, convocações, log). Operação irreversível. Apaga na ordem das
    tabelas filhas para a pai, para respeitar as chaves estrangeiras."""
    conn = db.get_conn()
    cur = conn.cursor()
    proc = cur.execute("SELECT * FROM processo WHERE id=?", (pid,)).fetchone()
    if not proc:
        conn.close()
        raise HTTPException(status_code=404, detail="Processo não encontrado.")
    nome = proc["nome"]

    # ids de curso e candidato deste processo (para apagar as tabelas sem processo_id)
    curso_ids = [r["id"] for r in cur.execute(
        "SELECT id FROM curso WHERE processo_id=?", (pid,)).fetchall()]
    cand_ids = []
    if curso_ids:
        marks = ",".join("?" * len(curso_ids))
        cand_ids = [r["id"] for r in cur.execute(
            f"SELECT id FROM candidato WHERE curso_id IN ({marks})", curso_ids).fetchall()]

    # apaga filhas -> pai
    cur.execute("DELETE FROM convocacao WHERE chamada_id IN (SELECT id FROM chamada WHERE processo_id=?)", (pid,))
    cur.execute("DELETE FROM log_decisao WHERE processo_id=?", (pid,))
    cur.execute("DELETE FROM chamada WHERE processo_id=?", (pid,))
    if curso_ids:
        marks = ",".join("?" * len(curso_ids))
        cur.execute(f"DELETE FROM vaga WHERE curso_id IN ({marks})", curso_ids)
    if cand_ids:
        marks = ",".join("?" * len(cand_ids))
        cur.execute(f"DELETE FROM elegibilidade WHERE candidato_id IN ({marks})", cand_ids)
    cur.execute("DELETE FROM candidato WHERE curso_id IN (SELECT id FROM curso WHERE processo_id=?)", (pid,))
    cur.execute("DELETE FROM curso WHERE processo_id=?", (pid,))
    cur.execute("DELETE FROM processo WHERE id=?", (pid,))

    conn.commit(); conn.close()
    return {"ok": True, "nome": nome}


@app.get("/api/processos/{pid}/cursos")
def cursos(pid: int):
    conn = db.get_conn()
    rows = conn.execute("""SELECT c.*,
        (SELECT COUNT(*) FROM candidato ca WHERE ca.curso_id=c.id) candidatos,
        (SELECT COUNT(*) FROM vaga v WHERE v.curso_id=c.id) vagas
        FROM curso c WHERE c.processo_id=? ORDER BY c.nome""", (pid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --------- Importação ---------------------------------------------------------
@app.post("/api/processos/{pid}/importar")
async def importar_planilha(pid: int, arquivo: UploadFile = File(...),
                            curso_nome: str = Form(None)):
    suffix = Path(arquivo.filename).suffix or ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await arquivo.read())
        caminho = tmp.name
    try:
        prev = importador.previa(caminho)
        curso_id, n = importador.importar(caminho, pid,
                                          curso_nome=curso_nome or prev["curso_sugerido"])
        # deriva as vagas (a 1ª chamada só depois da hetero)
        motor.derivar_vagas(pid, curso_id)
        return {"curso_id": curso_id, "importados": n,
                "curso": curso_nome or prev["curso_sugerido"],
                "classificados": prev["classificados"],
                "lista_espera": prev["lista_espera"],
                "vagas": importador.vagas_por_modalidade(curso_id)}
    finally:
        os.unlink(caminho)


# --------- Heteroidentificação ------------------------------------------------
@app.post("/api/processos/{pid}/hetero/convocar")
def hetero_convocar(pid: int):
    return motor.convocar_hetero(pid)


@app.get("/api/processos/{pid}/hetero")
def hetero_listar(pid: int):
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT cv.candidato_id, c.nome, c.inscricao, c.pos_lb_ppi, c.pos_li_ppi,
               e.resultado_hetero
        FROM convocacao cv
        JOIN chamada ch ON ch.id=cv.chamada_id AND ch.tipo='HETERO'
        JOIN candidato c ON c.id=cv.candidato_id
        JOIN elegibilidade e ON e.candidato_id=c.id
        WHERE ch.processo_id=? ORDER BY c.posicao_geral""", (pid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/hetero/{candidato_id}/resultado")
def hetero_resultado(candidato_id: int, resultado: str = Form(...)):
    motor.registrar_hetero(candidato_id, resultado)
    return {"ok": True}


# --------- Chamadas de matrícula ---------------------------------------------
@app.post("/api/processos/{pid}/primeira-chamada")
def primeira_chamada(pid: int):
    try:
        return motor.gerar_primeira_chamada(pid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/processos/{pid}/hetero-pendente")
def hetero_pendente(pid: int):
    return {"pendentes": motor.hetero_pendente(pid)}


@app.post("/api/processos/{pid}/chamada")
def nova_chamada(pid: int):
    return motor.gerar_chamada_matricula(pid)


@app.get("/api/processos/{pid}/chamadas")
def listar_chamadas(pid: int):
    conn = db.get_conn()
    chs = conn.execute("""SELECT * FROM chamada WHERE processo_id=?
                          ORDER BY tipo, numero""", (pid,)).fetchall()
    ultima_mat = conn.execute("""SELECT MAX(numero) n FROM chamada
                                 WHERE processo_id=? AND tipo='MATRICULA'""",
                              (pid,)).fetchone()["n"]
    out = []
    for ch in chs:
        convs = conn.execute("""
            SELECT cv.id, cv.candidato_id, cv.vaga_id, cv.modalidade_convocacao,
                   cv.resultado, c.nome, c.inscricao,
                   v.modalidade_original
            FROM convocacao cv
            JOIN candidato c ON c.id=cv.candidato_id
            LEFT JOIN vaga v ON v.id=cv.vaga_id
            WHERE cv.chamada_id=? ORDER BY cv.id""", (ch["id"],)).fetchall()
        corrigivel = (ch["tipo"] == "MATRICULA" and ch["numero"] == ultima_mat)
        # excluível: é a última chamada de matrícula E não é a 1ª (a 1ª é protegida)
        excluivel = (ch["tipo"] == "MATRICULA" and ch["numero"] == ultima_mat
                     and ch["numero"] > 1)
        out.append({**dict(ch), "corrigivel": corrigivel, "excluivel": excluivel,
                    "convocacoes": [dict(x) for x in convs]})
    conn.close()
    return out


@app.post("/api/convocacao/{cid}/resultado")
def convocacao_resultado(cid: int, resultado: str = Form(...)):
    motor.registrar_matricula(cid, resultado)
    return {"ok": True}


@app.post("/api/convocacao/{cid}/corrigir")
def convocacao_corrigir(cid: int):
    try:
        return motor.corrigir_convocacao(cid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/chamada/{chamada_id}/excluir")
def chamada_excluir(chamada_id: int):
    try:
        return motor.excluir_chamada(chamada_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --------- Vagas --------------------------------------------------------------
@app.get("/api/processos/{pid}/vagas")
def listar_vagas(pid: int):
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT v.*, c.nome cand_nome, cu.nome curso_nome
        FROM vaga v JOIN curso cu ON cu.id=v.curso_id
        LEFT JOIN candidato c ON c.id=v.candidato_atual_id
        WHERE cu.processo_id=? ORDER BY v.curso_id, v.modalidade_original, v.id""",
        (pid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/vaga/{vid}/liberar")
def liberar_vaga(vid: int):
    motor.liberar_vaga_pos_aulas(vid)
    return {"ok": True}


# --------- Rastreabilidade ----------------------------------------------------
@app.get("/api/processos/{pid}/log")
def log(pid: int, acao: str = None):
    conn = db.get_conn()
    q = """SELECT l.*, c.nome cand_nome FROM log_decisao l
           LEFT JOIN candidato c ON c.id=l.candidato_id
           WHERE l.processo_id=?"""
    params = [pid]
    if acao:
        q += " AND l.acao=?"; params.append(acao)
    q += " ORDER BY l.id DESC LIMIT 500"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --------- Regras de remanejamento (tabela única, editável) ------------------
@app.get("/api/remanejamento")
def remanejamento():
    return {"ordem": get_ordem_completa(),
            "modalidades": modalidades_disponiveis()}


@app.post("/api/remanejamento")
def salvar_remanejamento(ordem: str = Form(...)):
    """Recebe a ordem editada como JSON {origem: [destinos]} e regrava."""
    try:
        nova = json.loads(ordem)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ordem inválida (JSON malformado).")
    salvar_ordem(nova)
    return {"ok": True}


# --------- Candidato (ficha completa) ----------------------------------------
@app.get("/api/candidato/{cid}")
def candidato(cid: int):
    conn = db.get_conn()
    c = conn.execute("""SELECT c.*, e.* FROM candidato c
                        JOIN elegibilidade e ON e.candidato_id=c.id
                        WHERE c.id=?""", (cid,)).fetchone()
    logs = conn.execute("""SELECT * FROM log_decisao WHERE candidato_id=?
                           ORDER BY id""", (cid,)).fetchall()
    conn.close()
    if not c:
        raise HTTPException(404)
    return {"candidato": dict(c), "historico": [dict(l) for l in logs]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
