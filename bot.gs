/**
 * Bot de sono da bebê — Telegram + Google Apps Script + Google Sheets
 * Grátis, sem cartão, sem servidor. Os dados ficam na planilha.
 *
 * Comandos (também funcionam como texto solto, sem a barra):
 *  /dormir [noite|soneca] [local] [hora]   /noite   /soneca
 *  /dormiu  /despertou  /voltou  /acordou  /mamada [obs]  /nota texto
 *  /cancelar  /desfazer  /status  /hoje  /semana
 * Hora: "21:05", "21h05" ou "-15" (= 15 min atrás)
 */

// ═════════════════ CONFIG — preencha aqui ═════════════════
const TOKEN = 'COLE_O_TOKEN_DO_BOTFATHER';
const ALLOWED = [];            // ex.: [123456789, 987654321]  (teu ID e o dela)
const WEBAPP_URL = 'COLE_A_URL_DO_APP_DA_WEB';
const NOME = 'Nina';
const TZ = 'Europe/Madrid';
const LEMBRETES = [10, 25, 40];  // minutos depois do /dormir
const LATENCIA_PADRAO = 10;      // usada quando ninguém marca /dormiu
const NOITE_DESDE = 18, NOITE_ATE = 6;  // começar a dormir nesse intervalo = noite
const VIRADA = 6;                // o "dia" do sono vai das 06h às 06h
const AVISAR_OUTROS = true;      // replica as ações pros outros pais
// ══════════════════════════════════════════════════════════

const LOCAIS = {berco: 'berço', colo: 'colo', carrinho: 'carrinho', carro: 'carro', cama: 'cama',
  sling: 'sling', canguru: 'canguru', sofa: 'sofá', rede: 'rede', moises: 'moisés', peito: 'no peito'};
const COLS = {
  sessoes: ['id', 'tipo', 'local', 'inicio', 'adormeceu', 'estimado', 'acordou', 'status', 'chat_id'],
  eventos: ['id', 'sessao_id', 'tipo', 'ts', 'texto'],
  chats: ['chat_id'],
};
const ABERTOS = ['tentando', 'dormindo', 'despertou'];
const H = 3600e3, MIN = 60e3;

// ───────────────────────── Setup (rodar 1x pelo editor) ─────────────────────────
function setup() {
  const ss = SpreadsheetApp.getActive();
  props().setProperty('SS', ss.getId());
  for (const nome in COLS) {
    let sh = ss.getSheetByName(nome) || ss.insertSheet(nome);
    if (sh.getLastRow() === 0) {
      sh.getRange('A:Z').setNumberFormat('@');
      sh.appendRow(COLS[nome]);
      sh.setFrozenRows(1);
    }
  }
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'relatorioAuto')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('relatorioAuto').timeBased()
    .onWeekDay(ScriptApp.WeekDay.MONDAY).atHour(8).inTimezone(TZ).create();

  tg('setMyCommands', {commands: JSON.stringify([
    ['dormir', 'Vou colocar pra dormir'], ['dormiu', 'Pegou no sono'], ['despertou', 'Despertou no meio'],
    ['voltou', 'Voltou a dormir'], ['acordou', 'Acordou de vez'], ['soneca', 'Começar soneca'],
    ['mamada', 'Registrar mamada'], ['nota', 'Anotação'], ['status', 'Como tá agora'],
    ['hoje', 'Resumo de hoje'], ['semana', 'Gráficos da semana'], ['desfazer', 'Desfazer última'],
    ['cancelar', 'Cancelar'], ['ajuda', 'Ajuda'],
  ].map(([c, d]) => ({command: c, description: d})))});
  const r = tg('setWebhook', {url: WEBAPP_URL, drop_pending_updates: 'true'});
  Logger.log('Webhook: ' + JSON.stringify(r));
}

// ───────────────────────── Utilidades ─────────────────────────
function props() { return PropertiesService.getScriptProperties(); }
function iso(d) { return Utilities.formatDate(d, TZ, "yyyy-MM-dd'T'HH:mm:ssXXX"); }
function dt(s) { return s ? new Date(s) : null; }
function hm(d) { return Utilities.formatDate(d, TZ, 'HH:mm'); }
function dur(ms) {
  const m = Math.max(0, Math.floor(ms / MIN));
  return m >= 60 ? Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0') : m + ' min';
}
function fh(horas) { return dur(horas * H); }
function horaLocal(d) {
  return Number(Utilities.formatDate(d, TZ, 'H')) + Number(Utilities.formatDate(d, TZ, 'm')) / 60;
}
function diaRef(d) { return Utilities.formatDate(new Date(d.getTime() - VIRADA * H), TZ, 'yyyy-MM-dd'); }
function tipoAuto(d) { const h = horaLocal(d); return (h >= NOITE_DESDE || h < NOITE_ATE) ? 'noite' : 'soneca'; }
function norm(s) {
  return s.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[.,!?]/g, '');
}
function mean(a) { return a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0; }
function addDias(d, n) {
  return Utilities.formatDate(new Date(new Date(d + 'T12:00:00Z').getTime() + n * 24 * H), 'UTC', 'yyyy-MM-dd');
}
function rotulo(d) {
  const DIAS = ['dom', 'seg', 'ter', 'qua', 'qui', 'sex', 'sáb'];
  return DIAS[new Date(d + 'T12:00:00Z').getUTCDay()] + ' ' + d.slice(8, 10) + '/' + d.slice(5, 7);
}

function parseHora(args) {
  let ts = new Date(), explicito = false;
  const resto = [];
  for (const a of args) {
    const m = a.match(/^(\d{1,2})[:hH](\d{2})$/), n = a.match(/^-(\d+)(min|m)?$/);
    if (m && !explicito && +m[1] < 24 && +m[2] < 60) {
      const agora = new Date();
      ts = new Date(agora.getTime() + ((+m[1] * 60 + +m[2]) - horaLocal(agora) * 60) * MIN);
      ts.setSeconds(0, 0);
      if (ts.getTime() > agora.getTime() + MIN) ts = new Date(ts.getTime() - 24 * H);
      explicito = true;
    } else if (n && !explicito) {
      ts = new Date(Date.now() - +n[1] * MIN);
      explicito = true;
    } else resto.push(a);
  }
  return {ts, resto, explicito};
}

function extraiTipoLocal(args) {
  let tipo = null, local = null;
  for (const a of args) {
    const a2 = norm(a);
    if (a2 === 'noite') tipo = 'noite';
    else if (['soneca', 'cochilo', 'siesta'].includes(a2)) tipo = 'soneca';
    else if (LOCAIS[a2] && !local) local = LOCAIS[a2];
  }
  return {tipo, local};
}

// ───────────────────────── Planilha ─────────────────────────
function tab(nome) { return SpreadsheetApp.openById(props().getProperty('SS')).getSheetByName(nome); }

function ler(nome) {
  const cols = COLS[nome];
  return tab(nome).getDataRange().getValues().slice(1).map((r, i) => {
    const o = {_row: i + 2};
    cols.forEach((c, j) => { o[c] = r[j] === '' ? '' : String(r[j]); });
    return o;
  });
}

function inserir(nome, obj) {
  const cols = COLS[nome];
  if (cols[0] === 'id') obj.id = String(ler(nome).reduce((m, o) => Math.max(m, +o.id || 0), 0) + 1);
  tab(nome).appendRow(cols.map(c => (obj[c] === undefined || obj[c] === null) ? '' : String(obj[c])));
  return obj;
}

function atualizar(nome, obj) {
  const cols = COLS[nome];
  tab(nome).getRange(obj._row, 1, 1, cols.length)
    .setValues([cols.map(c => (obj[c] === undefined || obj[c] === null) ? '' : String(obj[c]))]);
}

function apagar(nome, obj) { tab(nome).deleteRow(obj._row); }

function sessaoAberta() {
  return ler('sessoes').filter(s => ABERTOS.includes(s.status)).sort((a, b) => b.id - a.id)[0] || null;
}
function ultimaFechada() {
  return ler('sessoes').filter(s => s.status === 'fim').sort((a, b) => dt(b.acordou) - dt(a.acordou))[0] || null;
}
function sessaoPorId(id) { return ler('sessoes').find(s => s.id == id) || null; }
function eventosDe(todos, sid) {
  return todos.filter(e => e.sessao_id == sid).sort((a, b) => (dt(a.ts) - dt(b.ts)) || (a.id - b.id));
}
function eventos(sid) { return eventosDe(ler('eventos'), sid); }
function addEvento(sid, tipo, ts, texto) {
  inserir('eventos', {sessao_id: sid || '', tipo: tipo, ts: iso(ts), texto: texto || ''});
}
function statusAtual() { const s = sessaoAberta(); return s ? s.status : null; }

// ───────────────────────── Cálculos ─────────────────────────
function blocos(s, evs) {
  if (!s.adormeceu) return [];
  const fim = s.acordou ? dt(s.acordou) : new Date();
  const out = [];
  let ini = dt(s.adormeceu);
  for (const e of evs) {
    const t = dt(e.ts);
    if (e.tipo === 'despertou' && ini) { out.push([ini, t]); ini = null; }
    else if (e.tipo === 'voltou' && !ini) ini = t;
  }
  if (ini) out.push([ini, fim]);
  return out.filter(([a, b]) => b > a);
}

function resumo(s, evs) {
  const bl = blocos(s, evs);
  const total = bl.reduce((t, [a, b]) => t + (b - a), 0);
  const ini = dt(s.adormeceu), fim = s.acordou ? dt(s.acordou) : new Date();
  let lat = null;
  if (ini && s.inicio && !s.estimado && ini > dt(s.inicio)) lat = (ini - dt(s.inicio)) / MIN;
  return {
    blocos: bl, total: total,
    acordada: ini ? (fim - ini - total) : 0,
    despertares: evs.filter(e => e.tipo === 'despertou').length,
    mamadas: evs.filter(e => e.tipo === 'mamada').length,
    maior: Math.max(0, ...bl.map(([a, b]) => b - a)),
    latencia: lat,
  };
}

function sessoesDoDia(ref) {
  return ler('sessoes')
    .filter(s => s.adormeceu && ['fim', 'dormindo', 'despertou'].includes(s.status) && diaRef(dt(s.adormeceu)) === ref)
    .sort((a, b) => dt(a.adormeceu) - dt(b.adormeceu));
}
function totalDoDia(ref) {
  const evs = ler('eventos');
  return sessoesDoDia(ref).reduce((t, s) => t + resumo(s, eventosDe(evs, s.id)).total, 0);
}

// ───────────────────────── Ações ─────────────────────────
function aDormir(ts, chat, args, tipo) {
  const s = sessaoAberta();
  if (s && s.status !== 'tentando')
    return NOME + ' já tá registrada dormindo desde ' + hm(dt(s.adormeceu)) + '. Se acordou, manda /acordou.';
  if (s) { cancelaLembretes(s.id); s.status = 'cancelada'; atualizar('sessoes', s); }
  const tl = extraiTipoLocal(args);
  tipo = tipo || tl.tipo || tipoAuto(ts);
  const nova = inserir('sessoes', {tipo: tipo, local: tl.local || '', inicio: iso(ts), status: 'tentando', chat_id: chat});
  agendaLembretes(nova.id, ts, chat);

  let txt = (tipo === 'noite' ? '🛏️ ' : '💤 ') + (tipo === 'noite' ? 'Noite' : 'Soneca') +
    ' começando às ' + hm(ts) + (tl.local ? ' (' + tl.local + ')' : '') + '.';
  const ult = ultimaFechada();
  if (ult && ts - dt(ult.acordou) < 12 * H && ts > dt(ult.acordou))
    txt += '\n⏳ Janela acordada: ' + dur(ts - dt(ult.acordou));
  txt += '\nTe aviso em ' + LEMBRETES.join(', ') + ' min.';
  return txt;
}

function aDormiu(ts, chat, args) {
  const s = sessaoAberta();
  const tl = extraiTipoLocal(args);
  if (!s) {
    const tipo = tl.tipo || tipoAuto(ts);
    inserir('sessoes', {tipo: tipo, local: tl.local || '', inicio: iso(ts), adormeceu: iso(ts), status: 'dormindo', chat_id: chat});
    return '😴 Anotado: ' + NOME + ' apagou às ' + hm(ts) + ' (' + tipo + (tl.local ? ', ' + tl.local : '') + ').';
  }
  if (s.status === 'despertou') return aVoltou(ts);
  if (s.status === 'dormindo') return 'Ela já tá marcada como dormindo 😉';
  cancelaLembretes(s.id);
  const ini = dt(s.inicio);
  if (ts < ini) ts = ini;
  s.adormeceu = iso(ts); s.status = 'dormindo';
  if (tl.local) s.local = tl.local;
  atualizar('sessoes', s);
  return '😴 ' + NOME + ' dormiu às ' + hm(ts) + ' — levou ' + dur(ts - ini) + ' pra pegar no sono.';
}

function aDespertou(ts) {
  const s = sessaoAberta();
  if (!s || s.status === 'tentando') return 'Ela ainda não tava marcada dormindo. Usa /dormiu primeiro (ou /cancelar).';
  if (s.status === 'despertou') return 'Já tá marcado que ela despertou. Quando voltar a dormir: /voltou';
  const bl = blocos(s, eventos(s.id));
  const iniBloco = bl.length ? bl[bl.length - 1][0] : dt(s.adormeceu);
  addEvento(s.id, 'despertou', ts);
  s.status = 'despertou'; atualizar('sessoes', s);
  return '🌙 Despertou às ' + hm(ts) + ' — tinha dormido ' + dur(ts - iniBloco) + ' seguidos.';
}

function aVoltou(ts) {
  const s = sessaoAberta();
  if (!s || s.status !== 'despertou') return 'Não tem despertar aberto. Se ela dormiu agora, usa /dormiu.';
  const d = eventos(s.id).filter(e => e.tipo === 'despertou').pop();
  addEvento(s.id, 'voltou', ts);
  s.status = 'dormindo'; atualizar('sessoes', s);
  return '😴 Voltou a dormir às ' + hm(ts) + ' — ficou ' + dur(ts - dt(d.ts)) + ' acordada.';
}

function aAcordou(ts, explicito) {
  let s = sessaoAberta();
  if (!s) return 'Não tem sono aberto. Manda /dormir (ou /dormiu se ela já apagou).';
  cancelaLembretes(s.id);
  if (s.status === 'tentando') {
    const ad = new Date(dt(s.inicio).getTime() + LATENCIA_PADRAO * MIN);
    if (ad >= ts) {
      s.status = 'cancelada'; atualizar('sessoes', s);
      return '🙃 Ela nem chegou a dormir — registrei como tentativa sem sucesso.';
    }
    s.adormeceu = iso(ad); s.estimado = '1';
  }
  if (s.status === 'despertou' && !explicito)
    ts = dt(eventos(s.id).filter(e => e.tipo === 'despertou').pop().ts);
  s.acordou = iso(ts); s.status = 'fim';
  atualizar('sessoes', s);

  const r = resumo(s, eventos(s.id));
  const ad = dt(s.adormeceu);
  const l = [
    '☀️ ' + NOME + ' acordou às ' + hm(ts),
    (s.tipo === 'noite' ? '🛏️ Noite' : '💤 Soneca') + (s.local ? ' · ' + s.local : ''),
    '😴 Dormiu ' + dur(r.total) + ' (' + hm(ad) + (s.estimado ? '*' : '') + ' → ' + hm(ts) + ')',
  ];
  if (r.despertares) {
    l.push('🌙 ' + r.despertares + ' despertar(es) · ' + dur(r.acordada) + ' acordada');
    l.push('🏆 Maior sono seguido: ' + dur(r.maior));
  }
  if (r.mamadas) l.push('🍼 ' + r.mamadas + ' mamada(s)');
  l.push('📅 Total do dia até agora: ' + dur(totalDoDia(diaRef(ad))));
  if (s.estimado) l.push('* horário estimado (início + ' + LATENCIA_PADRAO + ' min)');
  return l.join('\n');
}

function aMamada(ts, args) {
  const s = sessaoAberta();
  const obs = args.join(' ');
  addEvento(s ? s.id : '', 'mamada', ts, obs);
  return '🍼 Mamada às ' + hm(ts) + ' anotada' + (obs ? ' (' + obs + ')' : '') + '.';
}

function aNota(texto) {
  if (!texto) return 'Manda assim: /nota tá com o dente nascendo';
  const s = sessaoAberta();
  addEvento(s ? s.id : '', 'nota', new Date(), texto);
  return '📝 Nota salva.';
}

function aCancelar() {
  const s = sessaoAberta();
  if (!s) return 'Não tem nada aberto pra cancelar.';
  cancelaLembretes(s.id);
  s.status = 'cancelada'; atualizar('sessoes', s);
  return '❌ Cancelado. Nada foi contado.';
}

function aDesfazer() {
  const s = ler('sessoes').sort((a, b) => b.id - a.id)[0];
  if (!s) return 'Nada pra desfazer.';
  if (s.status === 'cancelada') {
    s.status = s.adormeceu ? 'dormindo' : 'tentando'; atualizar('sessoes', s);
    return '↩️ Desfeito: o cancelamento foi revertido.';
  }
  if (s.status === 'fim') {
    if (s.estimado) { s.adormeceu = ''; s.estimado = ''; s.status = 'tentando'; }
    else {
      const evs = eventos(s.id).filter(e => e.tipo === 'despertou' || e.tipo === 'voltou');
      s.status = evs.length && evs[evs.length - 1].tipo === 'despertou' ? 'despertou' : 'dormindo';
    }
    s.acordou = ''; atualizar('sessoes', s);
    return '↩️ Desfeito: ela voltou a constar como dormindo.';
  }
  if (s.status === 'tentando') {
    cancelaLembretes(s.id); apagar('sessoes', s);
    return '↩️ Desfeito: tentativa apagada.';
  }
  const ev = eventos(s.id).filter(e => e.tipo === 'despertou' || e.tipo === 'voltou').pop();
  if (ev) {
    apagar('eventos', ev);
    s.status = ev.tipo === 'despertou' ? 'dormindo' : 'despertou'; atualizar('sessoes', s);
    return '↩️ Desfeito: \'' + ev.tipo + '\' das ' + hm(dt(ev.ts)) + '.';
  }
  if (s.inicio === s.adormeceu) { apagar('sessoes', s); return '↩️ Desfeito: sono apagado.'; }
  s.adormeceu = ''; s.status = 'tentando'; atualizar('sessoes', s);
  return '↩️ Desfeito: voltou pra \'tentando dormir\'.';
}

function aStatus() {
  const s = sessaoAberta(), t = new Date();
  if (!s) {
    const u = ultimaFechada();
    return u ? '☀️ Acordada desde ' + hm(dt(u.acordou)) + ' (há ' + dur(t - dt(u.acordou)) + ').'
      : 'Nada registrado ainda. Manda /dormir quando for colocar ela pra dormir.';
  }
  if (s.status === 'tentando')
    return '⏳ Tentando dormir desde ' + hm(dt(s.inicio)) + ' (há ' + dur(t - dt(s.inicio)) + ').';
  const evs = eventos(s.id), r = resumo(s, evs);
  if (s.status === 'despertou') {
    const d = evs.filter(e => e.tipo === 'despertou').pop();
    return '🌙 Despertou às ' + hm(dt(d.ts)) + ' (há ' + dur(t - dt(d.ts)) + '). Dormiu ' + dur(r.total) + ' até agora.';
  }
  return '😴 Dormindo desde ' + hm(dt(s.adormeceu)) + '. Bloco atual: ' +
    dur(t - r.blocos[r.blocos.length - 1][0]) + ' · total: ' + dur(r.total) + '.';
}

function aHoje() {
  const ref = diaRef(new Date());
  const ss = sessoesDoDia(ref);
  if (!ss.length) return 'Nenhum sono registrado hoje ainda.';
  const evs = ler('eventos');
  const l = ['📅 Hoje (' + ref.slice(8, 10) + '/' + ref.slice(5, 7) + '):'];
  let tot = 0;
  for (const s of ss) {
    const r = resumo(s, eventosDe(evs, s.id));
    tot += r.total;
    l.push((s.tipo === 'noite' ? '🛏️ ' : '💤 ') + hm(dt(s.adormeceu)) + '–' +
      (s.acordou ? hm(dt(s.acordou)) : 'agora') + ': ' + dur(r.total) + (r.despertares ? ' · ' + r.despertares + '🌙' : ''));
  }
  l.push('Total: ' + dur(tot));
  return l.join('\n');
}

// ───────────────────────── Lembretes ─────────────────────────
function agendaLembretes(sid, inicio, chat) {
  LEMBRETES.forEach((m, i) => {
    const ms = inicio.getTime() + m * MIN - Date.now();
    if (ms > 0) {
      const t = ScriptApp.newTrigger('lembrete').timeBased().after(ms).create();
      props().setProperty('L_' + t.getUniqueId(), JSON.stringify({sid: sid, chat: chat, i: i, min: m}));
    }
  });
}

function cancelaLembretes(sid) {
  const all = props().getProperties();
  ScriptApp.getProjectTriggers().forEach(t => {
    const k = 'L_' + t.getUniqueId();
    if (all[k] && JSON.parse(all[k]).sid == sid) { ScriptApp.deleteTrigger(t); props().deleteProperty(k); }
  });
}

function lembrete(e) {
  const k = 'L_' + e.triggerUid;
  const d = JSON.parse(props().getProperty(k) || 'null');
  ScriptApp.getProjectTriggers().forEach(t => { if (t.getUniqueId() === e.triggerUid) ScriptApp.deleteTrigger(t); });
  props().deleteProperty(k);
  if (!d) return;
  const s = sessaoPorId(d.sid);
  if (!s || s.status !== 'tentando') return;
  let txt;
  if (d.i === 0) txt = '⏱️ ' + d.min + ' min — ' + NOME + ' deve estar começando a pegar no sono 🥱';
  else if (d.i < LEMBRETES.length - 1) txt = '⏱️ ' + d.min + ' min. Já dormiu?';
  else txt = '⏱️ ' + d.min + ' min. Se ela apagou, marca aí 👇 Se ainda tá acordada, força aí 💪';
  enviar(d.chat, txt, 'tentando');
}

// ───────────────────────── Relatório semanal ─────────────────────────
function estatisticas(dIni, dFim) {
  const dias = [];
  for (let i = 0; i < 31; i++) { const d = addDias(dIni, i); dias.push(d); if (d === dFim) break; }
  const por = {};
  dias.forEach(d => { por[d] = {noite: 0, soneca: 0, nSoneca: 0, despertares: 0, acordada: 0, lat: [], bedtime: null, maior: 0}; });
  const evAll = ler('eventos'), todos = [], mamadas = [];
  const ss = ler('sessoes').filter(s => s.adormeceu && ['fim', 'dormindo', 'despertou'].includes(s.status));
  for (const s of ss) {
    const evs = eventosDe(evAll, s.id), r = resumo(s, evs);
    r.blocos.forEach(([a, b]) => todos.push([a, b, s.tipo]));
    evs.filter(e => e.tipo === 'mamada').forEach(e => mamadas.push(dt(e.ts)));
    const p = por[diaRef(dt(s.adormeceu))];
    if (!p) continue;
    p[s.tipo] += r.total / H;
    if (r.latencia !== null) p.lat.push(r.latencia);
    if (s.tipo === 'soneca') p.nSoneca++;
    else {
      p.despertares += r.despertares;
      p.acordada += r.acordada / MIN;
      p.maior = Math.max(p.maior, r.maior / H);
      const b = dt(s.adormeceu);
      if (!p.bedtime || b < p.bedtime) p.bedtime = b;
    }
  }
  evAll.filter(e => e.tipo === 'mamada' && !e.sessao_id).forEach(e => mamadas.push(dt(e.ts)));
  return {dias: dias, por: por, todos: todos, mamadas: mamadas};
}

function horaDec(d) { const h = horaLocal(d); return h < 12 ? h + 24 : h; }
function r1(x) { return Math.round(x * 10) / 10; }

function chartPng(cfg, fns, w, h) {
  let s = JSON.stringify(cfg);
  for (const k in fns) s = s.split('"' + k + '"').join(fns[k]);
  const r = UrlFetchApp.fetch('https://quickchart.io/chart', {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    payload: JSON.stringify({chart: s, width: w || 800, height: h || 450, format: 'png',
      backgroundColor: 'white', version: '4'}),
  });
  return r.getBlob();
}

function graficos(st) {
  const COR_N = '#3b4cc0', COR_S = '#f4a259', COR_M = '#dc143c';
  const labels = st.dias.map(rotulo), curtos = labels.map(l => l.split(' ')[0]);
  const n = st.dias.length;

  // 1) Linha do tempo 06h → 06h
  const ds = {};
  st.dias.forEach((d, i) => {
    const base = Utilities.parseDate(d + ' ' + String(VIRADA).padStart(2, '0') + ':00', TZ, 'yyyy-MM-dd HH:mm');
    const topo = new Date(base.getTime() + 24 * H);
    const itens = [];
    st.todos.forEach(([a, b, t]) => {
      const a2 = Math.max(a, base), b2 = Math.min(b, topo);
      if (b2 > a2) itens.push({v: [(a2 - base) / H, (b2 - base) / H], t: t});
    });
    st.mamadas.filter(m => m >= base && m < topo)
      .forEach(m => { const x = (m - base) / H; itens.push({v: [x - 0.08, x + 0.08], t: 'mamada'}); });
    const cont = {};
    itens.forEach(it => {
      const k = cont[it.t] = (cont[it.t] || 0) + 1;
      const key = it.t + (k > 1 ? '#' + k : '');
      if (!ds[key]) ds[key] = {label: key, data: new Array(n).fill(null), grouped: false, borderSkipped: false,
        backgroundColor: it.t === 'noite' ? COR_N : it.t === 'soneca' ? COR_S : COR_M, barPercentage: 0.8};
      ds[key].data[i] = it.v;
    });
  });
  const tl = chartPng({
    type: 'bar', data: {labels: labels, datasets: Object.values(ds)},
    options: {indexAxis: 'y', plugins: {title: {display: true, text: 'Sono da ' + NOME + ' — linha do tempo'},
      legend: {position: 'bottom', labels: {filter: '__FILTER__'}}},
    scales: {x: {min: 0, max: 24, ticks: {stepSize: 2, callback: '__TICK__'}}}},
  }, {
    __FILTER__: 'function(i){return i.text.indexOf("#")<0}',
    __TICK__: 'function(v){return String((' + VIRADA + '+v)%24).padStart(2,"0")+"h"}',
  }, 800, 480);

  // 2) Total por dia
  const tot = chartPng({
    type: 'bar', data: {labels: curtos, datasets: [
      {label: 'noite', data: st.dias.map(d => r1(st.por[d].noite)), backgroundColor: COR_N},
      {label: 'soneca', data: st.dias.map(d => r1(st.por[d].soneca)), backgroundColor: COR_S}]},
    options: {plugins: {title: {display: true, text: 'Horas de sono por dia'}},
      scales: {x: {stacked: true}, y: {stacked: true, beginAtZero: true}}},
  });

  // 3) Despertares
  const desp = chartPng({
    type: 'bar', data: {labels: curtos, datasets: [
      {label: 'despertares', data: st.dias.map(d => st.por[d].despertares), backgroundColor: '#6c757d', yAxisID: 'y', order: 2},
      {type: 'line', label: 'min acordada', data: st.dias.map(d => Math.round(st.por[d].acordada)),
        borderColor: COR_M, backgroundColor: COR_M, yAxisID: 'y1', order: 1}]},
    options: {plugins: {title: {display: true, text: 'Despertares à noite'}},
      scales: {y: {beginAtZero: true, suggestedMax: 3, ticks: {precision: 0}}, y1: {beginAtZero: true, position: 'right', grid: {drawOnChartArea: false}}}},
  });

  // 4) Hora de dormir + tempo pra pegar no sono
  const bt = st.dias.map(d => st.por[d].bedtime ? r1(horaDec(st.por[d].bedtime)) : null);
  const vals = bt.filter(v => v !== null);
  const hora = chartPng({
    type: 'bar', data: {labels: curtos, datasets: [
      {type: 'line', label: 'hora que dormiu (noite)', data: bt, borderColor: COR_N, backgroundColor: COR_N, spanGaps: true, yAxisID: 'y', order: 1},
      {label: 'min pra pegar no sono', data: st.dias.map(d => Math.round(mean(st.por[d].lat))), backgroundColor: '#8ab17d', yAxisID: 'y1', order: 2}]},
    options: {plugins: {title: {display: true, text: 'Hora de dormir e tempo pra pegar no sono'}},
      scales: {
        y: {min: vals.length ? Math.floor(Math.min(...vals)) - 1 : 18, max: vals.length ? Math.ceil(Math.max(...vals)) + 1 : 24,
          ticks: {stepSize: 1, callback: '__HORA__'}},
        y1: {beginAtZero: true, position: 'right', grid: {drawOnChartArea: false}}}},
  }, {__HORA__: 'function(v){return String(v%24).padStart(2,"0")+"h"}'});

  return [tl, tot, desp, hora];
}

function textoSemana(st, prev) {
  const com = st.dias.filter(d => st.por[d].noite + st.por[d].soneca > 0);
  const noites = st.dias.filter(d => st.por[d].noite > 0);
  const tot = mean(com.map(d => st.por[d].noite + st.por[d].soneca));
  const l = ['📊 Semana da ' + NOME + ' (' + com.length + ' dias com registro)'];
  let linha = '😴 Média total: ' + fh(tot) + '/dia';
  const pc = prev.dias.filter(d => prev.por[d].noite + prev.por[d].soneca > 0);
  if (pc.length) {
    const diff = tot - mean(pc.map(d => prev.por[d].noite + prev.por[d].soneca));
    linha += ' (' + (diff >= 0 ? '+' : '−') + fh(Math.abs(diff)) + ' vs semana passada)';
  }
  l.push(linha);
  if (noites.length) {
    l.push('🛏️ Noite: ' + fh(mean(noites.map(d => st.por[d].noite))) + ' · ' +
      mean(noites.map(d => st.por[d].despertares)).toFixed(1) + ' despertares/noite');
    const melhor = noites.reduce((a, b) => st.por[b].maior > st.por[a].maior ? b : a);
    l.push('🏆 Maior sono seguido: ' + fh(st.por[melhor].maior) + ' (' + rotulo(melhor) + ')');
    const bts = noites.filter(d => st.por[d].bedtime).map(d => horaDec(st.por[d].bedtime));
    if (bts.length) {
      const m = mean(bts) % 24;
      l.push('🕗 Hora média de dormir: ' + String(Math.floor(m)).padStart(2, '0') + ':' + String(Math.floor(m % 1 * 60)).padStart(2, '0'));
    }
  }
  l.push('💤 Sonecas: ' + mean(com.map(d => st.por[d].nSoneca)).toFixed(1) + '/dia · ' +
    fh(mean(com.map(d => st.por[d].soneca))) + '/dia');
  const lats = [].concat(...st.dias.map(d => st.por[d].lat));
  if (lats.length) l.push('⏱️ Pra pegar no sono: ' + Math.round(mean(lats)) + ' min em média');
  return l.join('\n');
}

function enviaRelatorio(chat, dIni, dFim) {
  const st = estatisticas(dIni, dFim);
  if (!st.dias.some(d => st.por[d].noite + st.por[d].soneca > 0)) {
    enviar(chat, 'Ainda não tenho sono registrado nesse período 🤷');
    return;
  }
  const prev = estatisticas(addDias(dIni, -7), addDias(dFim, -7));
  const imgs = graficos(st);
  const payload = {chat_id: String(chat),
    media: JSON.stringify(imgs.map((b, i) => ({type: 'photo', media: 'attach://p' + i})))};
  imgs.forEach((b, i) => { payload['p' + i] = b.setName('p' + i + '.png'); });
  tg('sendMediaGroup', payload);
  enviar(chat, textoSemana(st, prev));
}

function relatorioAuto() {
  const fim = addDias(diaRef(new Date()), -1);  // segunda 08h: fecha seg→dom, com a noite de domingo
  ler('chats').forEach(c => enviaRelatorio(c.chat_id, addDias(fim, -6), fim));
}

// ───────────────────────── Telegram ─────────────────────────
function tg(method, payload) {
  const r = UrlFetchApp.fetch('https://api.telegram.org/bot' + TOKEN + '/' + method,
    {method: 'post', payload: payload, muteHttpExceptions: true});
  const j = JSON.parse(r.getContentText());
  if (!j.ok) console.error(method + ': ' + r.getContentText());
  return j;
}

function teclado(status) {
  const B = (t, d) => ({text: t, callback_data: d});
  if (status === 'tentando') return [[B('😴 Dormiu', 'dormiu'), B('❌ Desistiu', 'cancelar')]];
  if (status === 'dormindo') return [[B('🌙 Despertou', 'despertou'), B('☀️ Acordou de vez', 'acordou')], [B('🍼 Mamada', 'mamada')]];
  if (status === 'despertou') return [[B('😴 Voltou a dormir', 'voltou'), B('☀️ Acordou de vez', 'acordou')], [B('🍼 Mamada', 'mamada')]];
  return [[B('🛏️ Noite', 'noite'), B('💤 Soneca', 'soneca')]];
}

function enviar(chat, texto, status) {
  const p = {chat_id: String(chat), text: texto};
  if (status !== undefined) p.reply_markup = JSON.stringify({inline_keyboard: teclado(status)});
  return tg('sendMessage', p);
}

const PALAVRAS = {
  dormir: 'dormir', noite: 'noite', soneca: 'soneca', cochilo: 'soneca',
  dormiu: 'dormiu', apagou: 'dormiu', pegou: 'dormiu',
  despertou: 'despertou', chorou: 'despertou', voltou: 'voltou', acordou: 'acordou',
  mamada: 'mamada', mamou: 'mamada', mamadeira: 'mamada',
  cancela: 'cancelar', cancelar: 'cancelar', desistiu: 'cancelar', desisti: 'cancelar',
  desfazer: 'desfazer', desfaz: 'desfazer', ops: 'desfazer',
  status: 'status', hoje: 'hoje', semana: 'semana', ajuda: 'ajuda',
};
const MUDAM_ESTADO = ['dormir', 'noite', 'soneca', 'dormiu', 'despertou', 'voltou', 'acordou', 'mamada', 'cancelar', 'desfazer'];

function ajuda() {
  return '👶 Bot do sono da ' + NOME + '\n\n' +
    '/dormir [noite|soneca] [local] — tô indo colocar ela pra dormir\n' +
    '/dormiu — pegou no sono (sem /dormir antes = apagou do nada)\n' +
    '/despertou · /voltou — despertares no meio do sono\n' +
    '/acordou — acordou de vez, te mando o resumo\n' +
    '/mamada · /nota texto\n/cancelar · /desfazer\n/status · /hoje · /semana\n\n' +
    '⏰ Aceita hora: "acordou 06:40", "dormiu -15" (15 min atrás)\n' +
    '💬 Pode escrever sem barra: "ela dormiu no carro"\n' +
    '📍 Locais: ' + Array.from(new Set(Object.values(LOCAIS))).join(', ');
}

function executar(acao, args, user, chat) {
  if (!ler('chats').some(c => c.chat_id == chat)) inserir('chats', {chat_id: chat});
  if (acao === 'semana') {
    const hoje = diaRef(new Date());
    enviaRelatorio(chat, addDias(hoje, -6), hoje);
    return;
  }
  const p = parseHora(args);
  let txt;
  switch (acao) {
    case 'dormir': txt = aDormir(p.ts, chat, p.resto, null); break;
    case 'noite': case 'soneca': txt = aDormir(p.ts, chat, p.resto, acao); break;
    case 'dormiu': txt = aDormiu(p.ts, chat, p.resto); break;
    case 'despertou': txt = aDespertou(p.ts); break;
    case 'voltou': txt = aVoltou(p.ts); break;
    case 'acordou': txt = aAcordou(p.ts, p.explicito); break;
    case 'mamada': txt = aMamada(p.ts, p.resto); break;
    case 'nota': txt = aNota(args.join(' ')); break;
    case 'cancelar': txt = aCancelar(); break;
    case 'desfazer': txt = aDesfazer(); break;
    case 'status': txt = aStatus(); break;
    case 'hoje': txt = aHoje(); break;
    default: txt = ajuda();
  }
  const status = statusAtual();
  enviar(chat, txt, status);
  if (AVISAR_OUTROS && MUDAM_ESTADO.includes(acao)) {
    ler('chats').filter(c => c.chat_id != chat)
      .forEach(c => enviar(c.chat_id, '👤 ' + user.first_name + ': ' + txt, status));
  }
}

function trata(up) {
  if (up.callback_query) {
    const q = up.callback_query;
    tg('answerCallbackQuery', {callback_query_id: q.id});
    if (!ALLOWED.includes(q.from.id)) return;
    tg('editMessageReplyMarkup', {chat_id: String(q.message.chat.id), message_id: String(q.message.message_id)});
    executar(q.data, [], q.from, q.message.chat.id);
    return;
  }
  const m = up.message;
  if (!m || !m.text) return;
  const user = m.from, chat = m.chat.id;
  const palavras = m.text.trim().split(/\s+/);
  if (palavras[0].startsWith('/')) {
    const cmd = palavras[0].slice(1).split('@')[0].toLowerCase();
    if (cmd === 'start') { enviar(chat, ajuda() + '\n\nTeu ID: ' + user.id); return; }
    if (!ALLOWED.includes(user.id)) { enviar(chat, 'Sem permissão. Teu ID é ' + user.id + ' — coloca ele em ALLOWED.'); return; }
    executar(cmd, palavras.slice(1), user, chat);
    return;
  }
  if (!ALLOWED.includes(user.id)) { enviar(chat, 'Sem permissão. Teu ID é ' + user.id + ' — coloca ele em ALLOWED.'); return; }
  for (let i = 0; i < palavras.length; i++) {
    const a = PALAVRAS[norm(palavras[i])];
    if (a) { executar(a, palavras.slice(i + 1).concat(palavras.slice(0, i)), user, chat); return; }
  }
  executar('nota', palavras, user, chat);  // sem palavra-chave = nota
}

function doPost(e) {
  try {
    const up = JSON.parse(e.postData.contents);
    const cache = CacheService.getScriptCache();
    if (cache.get('u' + up.update_id)) return ok();  // Telegram às vezes reenvia
    cache.put('u' + up.update_id, '1', 21600);
    const lock = LockService.getScriptLock();
    lock.waitLock(25000);
    try { trata(up); } finally { lock.releaseLock(); }
  } catch (err) {
    console.error(err && err.stack || err);
  }
  return ok();
}

function ok() { return HtmlService.createHtmlOutput('ok'); }
function doGet() { return HtmlService.createHtmlOutput('Bot do sono no ar 👶'); }
