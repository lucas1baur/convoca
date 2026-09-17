// Convoca — lógica do frontend
let processoAtual = null;

const $ = s => document.querySelector(s);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (r.status === 401){ location.href = '/login'; throw new Error('Não autenticado.'); }
  if (!r.ok) { const t = await r.text(); throw new Error(t || r.status); }
  return r.status === 204 ? null : r.json();
};
const form = obj => { const f = new FormData(); for (const k in obj) f.append(k, obj[k]); return f; };

// Formata a data (gravada em ISO 'AAAA-MM-DD HH:MM:SS') para o padrão brasileiro
// 'DD/MM/AAAA HH:MM', sem segundos. Se algo vier fora do esperado, devolve como veio.
function dataBR(s){
  if (!s) return '';
  const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!m) return s;
  const [, ano, mes, dia, hh, mm] = m;
  return `${dia}/${mes}/${ano} ${hh}:${mm}`;
}

function toast(msg, erro=false){
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (erro?' erro':'');
  setTimeout(()=>t.classList.add('hidden'), 3200);
}
function modal(html){ $('#modalBody').innerHTML = html; $('#modal').classList.remove('hidden'); }
function fecharModal(){ $('#modal').classList.add('hidden'); }

// ---- Tabs ----
document.querySelectorAll('.tabs button').forEach(b=>{
  b.onclick = ()=>{
    document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    $('#'+b.dataset.tab).classList.add('active');
    carregarTab(b.dataset.tab);
  };
});

// ---- Processos ----
async function carregarProcessos(){
  const ps = await api('/api/processos');
  const sel = $('#processoSel');
  sel.innerHTML = ps.map(p=>`<option value="${p.id}">${p.nome}</option>`).join('');
  if (ps.length){ processoAtual = ps[0].id; sel.value = processoAtual; }
  else { processoAtual = null; }
  carregarTab('painel');
}
$('#processoSel').onchange = e => { processoAtual = +e.target.value; carregarTab(tabAtiva()); };
$('#novoProc').onclick = async ()=>{
  const nome = prompt('Nome do processo seletivo (ex.: PS 1048/2025 — Enfermagem):');
  if (!nome) return;
  const p = await api('/api/processos', {method:'POST', body:form({nome})});
  await carregarProcessos();
  $('#processoSel').value = p.id; processoAtual = p.id; carregarTab('painel');
  toast('Processo criado.');
};
$('#excluirProc').onclick = async ()=>{
  if (!processoAtual){ toast('Nenhum processo selecionado.', true); return; }
  const sel = $('#processoSel');
  const nome = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : 'este processo';
  if (!confirm(`Excluir “${nome}”? Isso apaga em definitivo todos os candidatos, `
    + 'vagas, chamadas e o histórico deste processo. Esta ação não pode ser desfeita.')) return;
  try{
    const r = await api(`/api/processos/${processoAtual}/excluir`, {method:'POST'});
    toast(`Processo “${r.nome}” excluído.`);
    await carregarProcessos();
  }catch(e){
    let msg = e.message;
    try{ msg = JSON.parse(e.message).detail || msg; }catch(_){}
    toast(msg, true);
  }
};

const tabAtiva = ()=> document.querySelector('.tabs button.active').dataset.tab;

function carregarTab(tab){
  if (!processoAtual && tab!=='regras'){ return; }
  ({painel:carregarPainel, hetero:carregarHetero, chamadas:carregarChamadas,
    vagas:carregarVagas, log:carregarLog, regras:carregarRegras}[tab] || (()=>{}))();
}

// ---- Painel / import ----
async function carregarPainel(){
  const cursos = await api(`/api/processos/${processoAtual}/cursos`);
  $('#cursosLista').innerHTML = cursos.length ? cursos.map(c=>`
    <div class="curso-card">
      <h3>${c.nome}</h3>
      <div class="nums">
        <span><b>${c.candidatos}</b>candidatos</span>
        <span><b>${c.vagas}</b>vagas</span>
      </div>
    </div>`).join('') : '<p class="vazio">Nenhum curso importado ainda.</p>';
}

$('#btnImportar').onclick = async ()=>{
  const f = $('#arquivo').files[0];
  if (!f){ toast('Selecione um arquivo .xlsx', true); return; }
  if (!processoAtual){ toast('Crie um processo primeiro.', true); return; }
  const fd = new FormData();
  fd.append('arquivo', f);
  if ($('#cursoNome').value) fd.append('curso_nome', $('#cursoNome').value);
  $('#btnImportar').disabled = true; $('#btnImportar').textContent = 'Importando…';
  try{
    const r = await api(`/api/processos/${processoAtual}/importar`, {method:'POST', body:fd});
    const vagasHtml = Object.entries(r.vagas).map(([m,n])=>`<span class="tarja t-carimbo">${m}: ${n}</span>`).join(' ');
    $('#importResumo').innerHTML = `
      <div class="panel" style="margin-top:16px;background:var(--verde-fundo);border-color:#bcdccb">
        <h2>Importado: ${r.curso}</h2>
        <p>${r.importados} candidatos (${r.classificados} classificados, ${r.lista_espera} em lista de espera).</p>
        <p><b>Vagas derivadas:</b> ${vagasHtml}</p>
        <p><b>Próximo passo:</b> vá em <b>Heteroidentificação</b>, convoque os PPI e lance os
           resultados. Só depois, na aba <b>Chamadas</b>, gere a 1ª chamada.</p>
      </div>`;
    toast('Planilha importada. Faça a heteroidentificação antes da 1ª chamada.');
    carregarPainel();
  }catch(e){ toast('Erro ao importar: '+e.message, true); }
  finally{ $('#btnImportar').disabled = false; $('#btnImportar').textContent = 'Importar planilha'; }
};

// ---- Heteroidentificação ----
let heteroDados = [];        // dados carregados da API (ordem original = posição)
let heteroOrdenacao = 'posicao'; // 'posicao' | 'nome'

async function carregarHetero(){
  const [dados, status] = await Promise.all([
    api(`/api/processos/${processoAtual}/hetero`),
    api(`/api/processos/${processoAtual}/hetero/status`)
  ]);
  heteroDados = dados;
  renderHeteroAcao(status);
  renderHetero();
}

function renderHeteroAcao(status){
  const acao = $('#heteroAcao');
  if (!status.convocada){
    // ainda não convocada: mostra o botão de convocar
    acao.innerHTML = `<button class="btn-primary" onclick="convocarHetero()">Convocar PPI (classificados + espera)</button>`;
  } else if (status.pode_refazer){
    // já convocada, mas a 1ª chamada ainda não foi gerada: pode refazer
    acao.innerHTML = `<button class="btn-excluir" onclick="refazerHetero()">↺ Refazer heteroidentificação</button>`;
  } else {
    // já convocada e a 1ª chamada já foi gerada: travada
    acao.innerHTML = `<span class="num" title="A 1ª chamada já foi gerada">heteroidentificação concluída</span>`;
  }
}

window.convocarHetero = async ()=>{
  try{
    const r = await api(`/api/processos/${processoAtual}/hetero/convocar`, {method:'POST'});
    toast(`${r.convocados.length} candidatos PPI convocados.`); carregarHetero();
  }catch(e){
    let msg = e.message;
    try{ msg = JSON.parse(e.message).detail || msg; }catch(_){}
    toast(msg, true);
  }
};

window.refazerHetero = async ()=>{
  if (!confirm('Refazer a heteroidentificação? A convocação atual e todos os '
    + 'resultados lançados (homologado/indeferido/ausente) serão apagados, e você '
    + 'convoca de novo do zero. Só é possível enquanto a 1ª chamada não foi gerada.')) return;
  try{
    await api(`/api/processos/${processoAtual}/hetero/excluir`, {method:'POST'});
    toast('Heteroidentificação desfeita. Convoque novamente.'); carregarHetero();
  }catch(e){
    let msg = e.message;
    try{ msg = JSON.parse(e.message).detail || msg; }catch(_){}
    toast(msg, true);
  }
};

function renderHetero(){
  const t = $('#heteroTabela');
  if (!heteroDados.length){ t.innerHTML = '<tr><td class="vazio">Ninguém convocado para heteroidentificação ainda.</td></tr>'; return; }

  // aplica a ordenação escolhida (cópia, sem alterar a ordem original)
  const rows = heteroDados.slice();
  if (heteroOrdenacao === 'nome'){
    rows.sort((a,b)=> (a.nome||'').localeCompare(b.nome||'', 'pt', {sensitivity:'base'}));
  }
  const seta = heteroOrdenacao === 'nome' ? ' ▲' : '';
  const dica = heteroOrdenacao === 'nome' ? 'Ordenado por nome (clique para voltar à ordem de posição)'
                                          : 'Clique para ordenar por nome';

  t.innerHTML = `<tr>
      <th class="th-clicavel" onclick="alternarOrdemHetero()" title="${dica}">Candidato${seta}</th>
      <th>Inscrição</th><th>LB_PPI</th><th>LI_PPI</th><th>Resultado</th><th>Ação</th></tr>` +
    rows.map(r=>`<tr>
      <td><span class="nome-link" onclick="ficha(${r.candidato_id})">${r.nome}</span></td>
      <td class="num">${r.inscricao}</td>
      <td class="num">${r.pos_lb_ppi ?? '—'}</td>
      <td class="num">${r.pos_li_ppi ?? '—'}</td>
      <td>${tarjaHetero(r.resultado_hetero)}</td>
      <td class="acoes-result">
        <button onclick="resHetero(${r.candidato_id},'HOMOLOGADO')">Homologar</button>
        <button onclick="resHetero(${r.candidato_id},'INDEFERIDO')">Indeferir</button>
        <button onclick="resHetero(${r.candidato_id},'AUSENTE')">Ausente</button>
      </td></tr>`).join('');
}

window.alternarOrdemHetero = ()=>{
  heteroOrdenacao = (heteroOrdenacao === 'nome') ? 'posicao' : 'nome';
  renderHetero();
};

function tarjaHetero(r){
  if (!r) return '<span class="tarja t-neutro">Pendente</span>';
  if (r==='HOMOLOGADO') return '<span class="tarja t-ok">Homologado</span>';
  return `<span class="tarja t-erro">${r[0]+r.slice(1).toLowerCase()}</span>`;
}
window.resHetero = async (id, res)=>{
  await api(`/api/hetero/${id}/resultado`, {method:'POST', body:form({resultado:res})});
  toast(`Registrado: ${res.toLowerCase()}.`); carregarHetero();
};

// ---- Chamadas ----
async function carregarChamadas(){
  const chs = await api(`/api/processos/${processoAtual}/chamadas`);
  const cont = $('#chamadasLista');
  const acao = $('#chamadasAcao');
  const matriculas = chs.filter(c=>c.tipo==='MATRICULA');

  if (!matriculas.length){
    // ainda não há 1ª chamada: o botão gera a 1ª (validando a hetero)
    acao.innerHTML = `<button class="btn-primary" onclick="gerarPrimeiraChamada()">Gerar 1ª chamada</button>`;
    cont.innerHTML = '<p class="vazio">Nenhuma chamada gerada. Faça a heteroidentificação e clique em “Gerar 1ª chamada”.</p>';
    return;
  }

  acao.innerHTML = `<button class="btn-primary" onclick="gerarProximaChamada()">Gerar próxima chamada</button>`;
  cont.innerHTML = matriculas.map(ch=>`
    <div class="chamada-bloco">
      <header>
        <h3>${ch.numero}ª chamada de matrícula</h3>
        <div style="display:flex;gap:10px;align-items:center">
          <span class="num">${dataBR(ch.criado_em)}</span>
          ${ch.excluivel ? `<button class="btn-excluir" onclick="excluirChamada(${ch.id}, ${ch.numero})">－ Excluir chamada</button>` : ''}
        </div>
      </header>
      <table class="grid">
        <tr><th>Candidato</th><th>Inscrição</th><th>Modalidade</th><th>Resultado</th><th>Registrar</th></tr>
        ${ch.convocacoes.map(cv=>linhaConv(cv, ch.corrigivel)).join('')}
      </table>
    </div>`).join('');
}
function linhaConv(cv, corrigivel){
  const reman = cv.modalidade_original && cv.modalidade_convocacao!==cv.modalidade_original
    ? ` <span class="tarja t-carimbo">remanej. de ${cv.modalidade_original}</span>` : '';
  let acoes;
  if (cv.resultado){
    acoes = corrigivel
      ? `<button class="btn-corrigir" onclick="corrigir(${cv.id})">↺ Corrigir</button>`
      : '<span class="num" title="Chamada selada por chamadas posteriores">publicado</span>';
  } else {
    acoes = `<div class="acoes-result">
        <button onclick="resMat(${cv.id},'HOMOLOGADO')">Homologar</button>
        <button onclick="resMat(${cv.id},'INDEFERIDO')">Indeferir</button>
        <button onclick="resMat(${cv.id},'NAO_COMPARECEU')">Não compareceu</button>
        <button onclick="resMat(${cv.id},'DESISTENTE')">Desistente</button>
      </div>`;
  }
  return `<tr>
    <td><span class="nome-link" onclick="ficha(${cv.candidato_id})">${cv.nome}</span></td>
    <td class="num">${cv.inscricao}</td>
    <td><span class="tarja ${cv.modalidade_convocacao==='AC'?'t-neutro':'t-carimbo'}">${cv.modalidade_convocacao}</span>${reman}</td>
    <td>${cv.resultado ? tarjaResultado(cv.resultado) : '<span class="tarja t-espera">Aguardando</span>'}</td>
    <td>${acoes}</td></tr>`;
}
function tarjaResultado(r){
  const map = {HOMOLOGADO:['t-ok','Homologado'], INDEFERIDO:['t-erro','Indeferido'],
    NAO_COMPARECEU:['t-erro','Não compareceu'], NAO_REALIZOU:['t-erro','Não realizou'],
    DESISTENTE:['t-erro','Desistente']};
  const [cls,txt] = map[r] || ['t-neutro', r];
  return `<span class="tarja ${cls}">${txt}</span>`;
}
window.resMat = async (id, res)=>{
  await api(`/api/convocacao/${id}/resultado`, {method:'POST', body:form({resultado:res})});
  toast(`Matrícula: ${res.toLowerCase()}.`); carregarChamadas();
};
window.corrigir = async (id)=>{
  if (!confirm('Desfazer o resultado registrado desta convocação? '
    + 'Todos os efeitos (matrícula, bloqueio de cotas, situação da vaga) serão revertidos, '
    + 'e a convocação volta para "Aguardando".')) return;
  try{
    const r = await api(`/api/convocacao/${id}/corrigir`, {method:'POST'});
    toast(`Resultado "${r.resultado_desfeito.toLowerCase()}" desfeito.`);
    carregarChamadas();
  }catch(e){
    let msg = e.message;
    try{ msg = JSON.parse(e.message).detail || msg; }catch(_){}
    toast(msg, true);
  }
};
window.gerarPrimeiraChamada = async ()=>{
  try{
    const r = await api(`/api/processos/${processoAtual}/primeira-chamada`, {method:'POST'});
    const preenchidas = r.convocacoes.filter(x=>x.candidato).length;
    toast(`1ª chamada gerada: ${preenchidas} convocações.`); carregarChamadas();
  }catch(e){
    let msg = e.message;
    try{ msg = JSON.parse(e.message).detail || msg; }catch(_){}
    toast(msg, true);
  }
};
window.gerarProximaChamada = async ()=>{
  const r = await api(`/api/processos/${processoAtual}/chamada`, {method:'POST'});
  const preenchidas = r.convocacoes.filter(x=>x.candidato).length;
  toast(`${r.numero}ª chamada: ${preenchidas} convocações geradas.`); carregarChamadas();
};
window.excluirChamada = async (id, numero)=>{
  if (!confirm(`Excluir a ${numero}ª chamada inteira? `
    + 'Todos os resultados registrados nela serão desfeitos (matrículas, eliminações, '
    + 'convocações), e as vagas voltam ao estado anterior. Esta ação não pode ser desfeita.')) return;
  try{
    const r = await api(`/api/chamada/${id}/excluir`, {method:'POST'});
    toast(`${r.numero_excluido}ª chamada excluída.`);
    carregarChamadas();
  }catch(e){
    let msg = e.message;
    try{ msg = JSON.parse(e.message).detail || msg; }catch(_){}
    toast(msg, true);
  }
};

// ---- Vagas ----
async function carregarVagas(){
  const vs = await api(`/api/processos/${processoAtual}/vagas`);
  const t = $('#vagasTabela');
  if (!vs.length){ t.innerHTML = '<tr><td class="vazio">Nenhuma vaga.</td></tr>'; return; }
  t.innerHTML = `<tr><th>#</th><th>Original</th><th>Atual</th><th>Últ. matrícula</th><th>Ocupante</th><th>Status</th><th>Ação</th></tr>` +
    vs.map(v=>`<tr>
      <td class="num">${v.id}</td>
      <td><span class="tarja t-neutro">${v.modalidade_original}</span></td>
      <td><span class="tarja t-carimbo">${v.modalidade_atual}</span></td>
      <td>${v.modalidade_ultima_matricula ? `<span class="tarja t-ok">${v.modalidade_ultima_matricula}</span>` : '—'}</td>
      <td>${v.cand_nome ? `<span class="nome-link" onclick="ficha(${v.candidato_atual_id})">${v.cand_nome}</span>` : '<span class="vazio">livre</span>'}</td>
      <td>${statusVaga(v.status, v.sem_elegivel)}</td>
      <td>${v.status==='PREENCHIDA' ? `<button class="btn-liberar" onclick="liberar(${v.id})">Liberar (pós-aulas)</button>` : '—'}</td>
    </tr>`).join('');
}
function statusVaga(s, semElegivel){
  // vaga livre que esgotou a lista de espera ganha um aviso próprio
  if (semElegivel && (s==='ABERTA' || s==='LIBERADA')){
    return `<span class="tarja t-erro" title="Não há mais candidatos elegíveis para esta vaga no momento">Sem elegível (lista esgotada)</span>`;
  }
  const m = {ABERTA:['t-espera','Aberta'], PREENCHIDA:['t-ok','Preenchida'], LIBERADA:['t-carimbo','Liberada']};
  const [c,t] = m[s]||['t-neutro',s]; return `<span class="tarja ${c}">${t}</span>`;
}
window.liberar = async (id)=>{
  if (!confirm('Liberar esta vaga (perda por 10 dias úteis letivos)? Reabrirá pela modalidade da última matrícula.')) return;
  await api(`/api/vaga/${id}/liberar`, {method:'POST'});
  toast('Vaga liberada.'); carregarVagas();
};

// ---- Log ----
let logDados = [];   // registros carregados do servidor (já filtrados por ação/chamada)

async function carregarLog(){
  // popula o seletor de chamadas com os números existentes (uma vez por carga)
  try{
    const nums = await api(`/api/processos/${processoAtual}/chamadas-numeros`);
    const selC = $('#logChamada');
    const atual = selC.value;
    selC.innerHTML = '<option value="">Todas as chamadas</option>' +
      nums.map(n=>`<option value="${n}">${n}ª chamada</option>`).join('');
    selC.value = atual; // preserva a seleção se ainda existir
  }catch(_){}

  const acao = $('#logFiltro').value;
  const chamada = $('#logChamada').value;
  let url = `/api/processos/${processoAtual}/log`;
  const qs = [];
  if (acao) qs.push(`acao=${acao}`);
  if (chamada) qs.push(`chamada=${chamada}`);
  if (qs.length) url += '?' + qs.join('&');

  logDados = await api(url);
  renderLog();
}

function renderLog(){
  const busca = ($('#logBusca').value || '').trim().toLowerCase();
  const cont = $('#logLista');
  // filtro por nome acontece aqui, na tela, sobre o que veio do servidor
  const logs = busca
    ? logDados.filter(l => (l.cand_nome || '').toLowerCase().includes(busca))
    : logDados;

  if (!logs.length){ cont.innerHTML = '<p class="vazio">Sem registros.</p>'; return; }
  cont.innerHTML = logs.map(l=>{
    // só as linhas de chamada de MATRÍCULA ganham o sufixo com o número da chamada
    const sufixoChamada = (l.chamada_tipo === 'MATRICULA' && l.chamada_numero)
      ? ` <span class="log-chamada">(${l.chamada_numero}ª chamada)</span>` : '';
    return `
    <div class="log-item">
      <div><span class="log-acao a-${l.acao}">${l.acao.replace(/_/g,' ')}</span></div>
      <div class="log-motivo">${l.cand_nome?`<b>${l.cand_nome}</b> — `:''}${l.motivo}${sufixoChamada}
        <div class="num" style="font-size:.75rem;color:var(--tinta-suave)">${dataBR(l.criado_em)}</div>
      </div>
    </div>`;
  }).join('');
}

// Filtros SEPARADOS: mexer num zera os outros dois, e recarrega.
$('#logFiltro').onchange = ()=>{
  $('#logChamada').value = '';
  $('#logBusca').value = '';
  carregarLog();
};
$('#logChamada').onchange = ()=>{
  $('#logFiltro').value = '';
  $('#logBusca').value = '';
  carregarLog();
};
$('#logBusca').oninput = ()=>{
  // ao começar a buscar por nome, zera os selects e filtra na tela (sem recarregar)
  if ($('#logBusca').value){
    if ($('#logFiltro').value || $('#logChamada').value){
      $('#logFiltro').value = '';
      $('#logChamada').value = '';
      carregarLog();  // recarrega tudo (sem filtros de servidor) e depois filtra por nome
      return;
    }
  }
  renderLog();
};

// ---- Regras (tabela única, editável) ----
let regrasOrdem = {};      // {origem: [destinos]}
let regrasModalidades = []; // modalidades válidas p/ os seletores
let regrasEditando = false;
const MAX_PASSOS = 8;

async function carregarRegras(){
  const r = await api('/api/remanejamento');
  regrasOrdem = r.ordem;
  regrasModalidades = r.modalidades;
  regrasEditando = false;
  renderRegras();
}

function renderRegras(){
  const origens = Object.keys(regrasOrdem);
  const head = `<tr><th>Vaga sobra em</th>${[...Array(MAX_PASSOS)].map((_,i)=>`<th>${i+1}º</th>`).join('')}</tr>`;
  let corpo;
  if (!regrasEditando){
    corpo = origens.map(m=>`<tr><td>${m}</td>${
      regrasOrdem[m].map(d=>`<td>${d}</td>`).join('')
    }${'<td>—</td>'.repeat(Math.max(0,MAX_PASSOS-regrasOrdem[m].length))}</tr>`).join('');
  } else {
    const opc = (sel)=>`<option value="">—</option>` +
      regrasModalidades.map(m=>`<option value="${m}" ${m===sel?'selected':''}>${m}</option>`).join('');
    corpo = origens.map(m=>{
      const destinos = regrasOrdem[m];
      const cels = [...Array(MAX_PASSOS)].map((_,i)=>
        `<td><select data-origem="${m}" data-passo="${i}">${opc(destinos[i]||'')}</select></td>`).join('');
      return `<tr><td>${m}</td>${cels}</tr>`;
    }).join('');
  }
  $('#ordemTabela').innerHTML = head + corpo;

  $('#regrasAcoes').innerHTML = regrasEditando
    ? `<button class="btn-primary" onclick="salvarRegras()">Salvar alterações</button>
       <button class="btn-ghost" onclick="carregarRegras()">Cancelar</button>`
    : `<button class="btn-ghost" onclick="editarRegras()">Editar tabela</button>`;
}

window.editarRegras = ()=>{ regrasEditando = true; renderRegras(); };

window.salvarRegras = async ()=>{
  // lê os selects e monta a nova ordem
  const nova = {};
  Object.keys(regrasOrdem).forEach(m=>nova[m]=[]);
  document.querySelectorAll('#ordemTabela select').forEach(sel=>{
    const origem = sel.dataset.origem;
    const val = sel.value;
    if (val) nova[origem].push(val);
  });
  await api('/api/remanejamento', {method:'POST', body:form({ordem: JSON.stringify(nova)})});
  toast('Tabela de remanejamento salva.');
  carregarRegras();
};

// ---- Ficha do candidato ----
window.ficha = async (id)=>{
  const d = await api(`/api/candidato/${id}`);
  const c = d.candidato;
  const pos = ['ac','li_ep','li_ppi','li_q','li_pcd','lb_ep','lb_ppi','lb_q','lb_pcd','v_efa','v_pcd']
    .filter(k=>c['pos_'+k]!=null).map(k=>`${k.toUpperCase()}: ${c['pos_'+k]}`).join(' · ') || '—';
  modal(`
    <h2>${c.nome}</h2>
    <div class="ficha-linha"><span>Inscrição</span><span>${c.inscricao}</span></div>
    <div class="ficha-linha"><span>Situação banca</span><span>${c.situacao_banca}</span></div>
    <div class="ficha-linha"><span>Modalidade inscrição</span><span>${c.modalidade_inscricao||'—'}</span></div>
    <div class="ficha-linha"><span>Modalidade classificação</span><span>${c.modalidade_classificacao||'—'}</span></div>
    <div class="ficha-linha"><span>Nota</span><span>${c.nota??'—'}</span></div>
    <div class="ficha-linha"><span>Classificação original (posições)</span><span>${pos}</span></div>
    <div class="ficha-linha"><span>Heteroidentificação</span><span>${c.resultado_hetero||'não convocado'}</span></div>
    <div class="ficha-linha"><span>Cotas bloqueadas</span><span>${c.cotas_bloqueadas?'SIM':'não'}</span></div>
    <div class="ficha-linha"><span>Situação atual</span><span><b>${c.situacao_atual}</b></span></div>
    <h2 style="font-size:1rem;margin-top:20px">Histórico</h2>
    ${d.historico.length ? d.historico.map(h=>{
      const sufixoChamada = (h.chamada_tipo === 'MATRICULA' && h.chamada_numero)
        ? ` <span class="log-chamada">(${h.chamada_numero}ª chamada)</span>` : '';
      return `<div class="log-item"><div><span class="log-acao a-${h.acao}">${h.acao.replace(/_/g,' ')}</span></div><div>${h.motivo}${sufixoChamada}<div class="num" style="font-size:.72rem;color:var(--tinta-suave)">${dataBR(h.criado_em)}</div></div></div>`;
    }).join('') : '<p class="vazio">Sem histórico.</p>'}
  `);
};

carregarProcessos();
