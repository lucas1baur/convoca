# Convoca — Controle de Chamadas do Processo Seletivo

Protótipo funcional de um sistema local para gerenciar convocações, matrículas,
heteroidentificação e remanejamento de vagas, a partir da planilha oficial da banca.

O sistema **não reclassifica** candidatos: a classificação da banca é a base
permanente. O sistema administra elegibilidade, convocações, bloqueios de cota,
remanejamentos e o histórico de cada candidato e de cada vaga.

---

## Como rodar (Windows / Mac / Linux)

Requer Python 3.10 ou superior instalado.

```bash
# 1. Instalar dependências (só na primeira vez)
pip install -r requirements.txt

# 2. Iniciar
python iniciar.py
```

O navegador abre sozinho em `http://127.0.0.1:8000`. Os dados ficam num arquivo
local `convoca.db` (SQLite) na mesma pasta. Para começar do zero, apague esse arquivo.

---

## Fluxo de uso

1. **Painel** — Crie um processo (botão "+ Processo") e importe a planilha .xlsx.
   O sistema transcreve a classificação, deriva as vagas (uma por classificado,
   na modalidade em que a banca o classificou) e gera a **1ª chamada** automaticamente.
2. **Heteroidentificação** — Convoque os PPI (LB_PPI/LI_PPI), classificados e em
   espera. Registre Homologado / Indeferido / Ausente. Indeferido ou ausente
   bloqueia todas as cotas e mantém só a AC.
3. **Chamadas** — Registre o resultado de cada convocado. Quando sobrarem vagas,
   gere a próxima chamada: da 2ª em diante o **motor** busca o próximo elegível na
   fila e remaneja quando necessário.
4. **Vagas** — Acompanhe modalidade original / atual / última matrícula. Uma vaga
   preenchida pode ser **liberada** (perda por 10 dias úteis letivos); ela reabre
   pela modalidade da última matrícula.
5. **Rastreabilidade** — Toda decisão fica registrada com o motivo.
6. **Regras** — Escolha a versão vigente da ordem de remanejamento. Como é
   legislação e pode mudar, a ordem é parametrizável e versionada.

---

## Arquitetura

```
app/
  db.py             Esquema SQLite. Separa classificação ORIGINAL (imutável)
                    de elegibilidade ATUAL (mutável). Vagas com vida própria.
  importador.py     Lê a planilha da banca; só transcreve, nunca reclassifica.
  remanejamento.py  Ordem de remanejamento parametrizável e versionada
                    (versões DOCX-§27 e PPTX-2.3.8 semeadas).
  motor.py          Coração: heteroidentificação, 1ª chamada (transcrição),
                    chamadas seguintes (busca do próximo elegível + remanejamento),
                    resultado da matrícula, vaga reaberta pós-aulas. Registra o log.
  main.py           API web (FastAPI) + serve a interface.
  static/, templates/  Interface web.
iniciar.py          Inicia o servidor e abre o navegador.
teste.py            Validação end-to-end com a planilha de Enfermagem.
```

### Decisões importantes (e por quê)

- **A 1ª chamada é transcrição, não cálculo.** A planilha do IFNMG já vem com a
  banca tendo remanejado a primeira distribuição (ex.: inscrito LB_PPI classificado
  em LB_EP). O sistema respeita isso: os `CLASSIFICADO` viram as convocações da 1ª
  chamada, cada um na sua modalidade de classificação. O motor de remanejamento só
  age da 2ª chamada em diante.
- **As colunas AC, LI_EP, LB_PPI… são as filas de espera** de cada modalidade,
  usadas a partir da 2ª chamada. Quem foi classificado direto em AC sai de todas as
  filas de cota (regra do cotista absorvido pela AC).
- **Chave do candidato = INSCRIÇÃO**, porque o CPF vem mascarado na planilha.
- **Vagas derivadas dos classificados** (uma por classificado).

### Pontos que dependem de confirmação externa

- **Ordem de remanejamento oficial:** há divergência entre o DOCX (§27) e o PPTX
  (2.3.8) na posição de LB_Q e LB_PcD. Ambas estão cadastradas; confirme com a
  banca/edital qual vigora e selecione em "Regras".
- **Número de vagas ofertadas:** hoje é derivado dos classificados. Se o edital
  ofertar um total diferente do que a banca classificou, isso precisará ser um
  ajuste manual (não implementado neste protótipo).

## O que este protótipo ainda NÃO faz

- Múltiplos cursos por processo com telas separadas (o motor já suporta vários
  cursos no mesmo banco; a interface foca em um por vez).
- Exportação das convocações para Excel/PDF (fácil de acrescentar).
- Edição manual da matriz de remanejamento pela tela (hoje troca-se de versão;
  criar/editar versão nova é via banco).
- Autenticação de usuários.
