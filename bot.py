"""
Bot de Telegram pra acompanhar o sono da bebê.

Comandos (também funcionam como texto solto, sem a barra):
  /dormir [noite|soneca] [local] [hora]  → começa a tentativa e agenda os lembretes
  /noite, /soneca                        → atalhos do /dormir
  /dormiu [hora]      → marca que pegou no sono (se não teve /dormir, cria o sono direto)
  /despertou [hora]   → acordou no meio do sono (vai tentar voltar)
  /voltou [hora]      → voltou a dormir
  /acordou [hora]     → acordou de vez; fecha o sono e manda o resumo
  /nota texto         → anotação livre (dente, febre, vacina...)
  /cancelar           → desistiu / não dormiu
  /desfazer           → desfaz a última ação de sono
  /status  /hoje  /semana

Hora pode ser "21:05", "21h05" ou "-15" (= 15 min atrás).
"""
import io
import logging
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, time as dtime
from statistics import mean
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

# ───────────────────────── Config ─────────────────────────
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()}
NOME = os.environ.get("BEBE_NOME", "Nina")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Madrid"))
DB_PATH = os.environ.get("DB_PATH", "sono.db")
LEMBRETES = [int(x) for x in os.environ.get("LEMBRETES", "10,25,40").split(",")]
LATENCIA_PADRAO = int(os.environ.get("LATENCIA_PADRAO", "10"))  # usada quando ninguém marca /dormiu
NOITE_DESDE, NOITE_ATE = 18, 6          # começar a dormir nesse intervalo = "noite"
VIRADA_DO_DIA = 6                        # o "dia" do sono vai das 06h às 06h
AVISAR_OUTROS = os.environ.get("AVISAR_OUTROS", "1") == "1"  # replica as ações pros outros pais

LOCAIS = {"berco": "berço", "colo": "colo", "carrinho": "carrinho", "carro": "carro", "cama": "cama",
          "sling": "sling", "canguru": "canguru", "sofa": "sofá", "rede": "rede", "moises": "moisés",
          "peito": "no peito"}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("sono")


# ───────────────────────── Utilidades ─────────────────────────
def agora():
    return datetime.now(TZ)


def dt(s):
    return datetime.fromisoformat(s).astimezone(TZ) if s else None


def hm(d):
    return d.astimezone(TZ).strftime("%H:%M")


def dur(td):
    m = max(0, int(td.total_seconds() // 60))
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m} min"


def fh(horas):
    return dur(timedelta(hours=horas))


def norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch)).strip(".,!?")


def dia_ref(d):
    return (d.astimezone(TZ) - timedelta(hours=VIRADA_DO_DIA)).date()


def tipo_auto(d):
    return "noite" if d.hour >= NOITE_DESDE or d.hour < NOITE_ATE else "soneca"


HORA_RE = re.compile(r"^(\d{1,2})[:hH](\d{2})$")
MENOS_RE = re.compile(r"^-(\d+)(min|m)?$")


def parse_hora(args):
    """Procura '21:05', '21h05' ou '-15' nos args. Devolve (datetime, resto, foi_explicito)."""
    ts, resto, explicito = agora(), [], False
    for a in args:
        m, n = HORA_RE.match(a), MENOS_RE.match(a)
        if m and not explicito and int(m[1]) < 24 and int(m[2]) < 60:
            ts = ts.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
            if ts > agora() + timedelta(minutes=1):
                ts -= timedelta(days=1)  # "acordou 23:50" dito à 00:10 = ontem
            explicito = True
        elif n and not explicito:
            ts = agora() - timedelta(minutes=int(n[1]))
            explicito = True
        else:
            resto.append(a)
    return ts, resto, explicito


def extrai_tipo_local(args):
    tipo, local = None, None
    for a in args:
        a2 = norm(a)
        if a2 in ("noite",):
            tipo = "noite"
        elif a2 in ("soneca", "cochilo", "siesta"):
            tipo = "soneca"
        elif a2 in LOCAIS and not local:
            local = LOCAIS[a2]
    return tipo, local


# ───────────────────────── Piadinhas ─────────────────────────
import random  # noqa: E402

FRASES = {
    "dormir": [
        "🛏️ Operação Nana iniciada às {h}. Que a força do sono esteja com vocês.",
        "🛏️ {h}: modo ninja ativado. Pisa leve e não espirra 🥷",
        "🛏️ Lá vamos nós às {h}. Boa sorte, soldado 🫡",
        "🛏️ {h}: início da missão. Canta baixinho, balança devagar, reza forte 🙏",
    ],
    "l_primeiro": [
        "⏱️ {m} min. Pálpebras pesando? {n} entrando em modo avião ✈️",
        "⏱️ {m} min. Fase 1: bocejo. Fase 2: olhinho piscando. Fase 3: 🙏",
        "⏱️ {m} min. Se ela bocejou, finge que não viu e segue o plano 🤫",
    ],
    "l_meio": [
        "⏱️ {m} min. E aí, apagou ou tá negociando com você? 🤝",
        "⏱️ {m} min. Relatório de campo, por favor 📋",
        "⏱️ {m} min. Já dormiu ou tá só fingindo pra te testar? 🕵️",
    ],
    "l_ultimo": [
        "⏱️ {m} min. Se ela dormiu, marca aí. Se não... bebe uma água, você consegue 💪",
        "⏱️ {m} min. Ela tá ganhando essa rodada, mas o jogo não acabou ⚽",
        "⏱️ {m} min. Resistência de atleta olímpica essa menina 🏅 Força aí!",
    ],
    "dormiu": [
        "😴 Nocaute às {h}! Levou {lat}. Agora sai de fininho igual ninja 🥷",
        "😴 {n} dormiu às {h} (levou {lat}). Não respira muito alto.",
        "😴 Apagou às {h}, depois de {lat}. Missão cumprida, soldado 🎖️",
        "😴 {h}: dormiu! {lat} de luta. Pode comemorar... em silêncio 🤐",
    ],
    "dormiu_surpresa": [
        "😴 {n} apagou às {h} sem avisar ninguém ({det}). Pegou todo mundo de surpresa 😅",
        "😴 Anotado: dormiu às {h} ({det}). Ninguém viu, ninguém ouviu 🫣",
    ],
    "despertou": [
        "🌙 {h}: {n} ligou pro SAC. Tinha dormido {b} seguidos.",
        "🌙 Alarme às {h}! Foram {b} de sono antes de convocar a gerência.",
        "🌙 Despertou às {h} depois de {b}. Plantão noturno ativado ☕",
    ],
    "voltou": [
        "😴 Voltou às {h}! Foram {a} de expediente. Pode voltar pra cama (ou tentar).",
        "😴 {h}: dormiu de novo depois de {a} acordada. Recua devagar 🚶",
        "😴 E voltou às {h}! {a} de hora extra. Boa, equipe 👏",
    ],
    "acordou": [
        "☀️ Bom dia, flor do dia! {n} acordou às {h}",
        "☀️ {h}: a chefe acordou. Todos em posição!",
        "☀️ {n} acordou às {h}. O show vai começar 🎬",
        "☀️ {h}: fim do intervalo, {n} de volta ao expediente.",
    ],
    "sem_despertar": ["📸 Zero despertares. Guarda esse dia na memória!", "🏆 Sono ininterrupto. Lenda."],
    "muitos_despertares": ["☕☕ Noite agitada... café duplo recomendado.", "🫠 Noite de balada. Força, time."],
    "nem_dormiu": [
        "🙃 Ela nem chegou a dormir. Registrado como 'tentativa heroica'.",
        "🙃 Não rolou. Ela 1 x 0 vocês. Tem revanche 🥊",
    ],
    "cancelar": [
        "❌ Cancelado. Ela venceu essa rodada 🥊",
        "❌ Missão abortada. Recalculando rota... 🗺️",
        "❌ Cancelado. Faz parte, até o Messi erra pênalti ⚽",
    ],
}


def frase(chave, **kw):
    return random.choice(FRASES[chave]).format(n=NOME, **kw)


# ───────────────────────── Banco ─────────────────────────
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessoes(
            id INTEGER PRIMARY KEY,
            tipo TEXT, local TEXT,
            inicio TEXT, adormeceu TEXT, estimado INTEGER DEFAULT 0,
            acordou TEXT, status TEXT, chat_id INTEGER);
        CREATE TABLE IF NOT EXISTS eventos(
            id INTEGER PRIMARY KEY, sessao_id INTEGER, tipo TEXT, ts TEXT, texto TEXT);
        CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY);
        """)


ABERTOS = "('tentando','dormindo','despertou')"


def sessao_aberta(c):
    return c.execute(f"SELECT * FROM sessoes WHERE status IN {ABERTOS} ORDER BY id DESC LIMIT 1").fetchone()


def ultima_fechada(c):
    return c.execute("SELECT * FROM sessoes WHERE status='fim' ORDER BY acordou DESC LIMIT 1").fetchone()


def eventos(c, sid):
    return c.execute("SELECT * FROM eventos WHERE sessao_id=? ORDER BY ts, id", (sid,)).fetchall()


def add_evento(c, sid, tipo, ts, texto=None):
    c.execute("INSERT INTO eventos(sessao_id,tipo,ts,texto) VALUES(?,?,?,?)", (sid, tipo, ts.isoformat(), texto))


def status_atual(c):
    s = sessao_aberta(c)
    return s["status"] if s else None


# ───────────────────────── Cálculos ─────────────────────────
def blocos(s, evs):
    """Trechos contínuos de sono (descontando os despertares)."""
    if not s["adormeceu"]:
        return []
    fim = dt(s["acordou"]) or agora()
    out, ini = [], dt(s["adormeceu"])
    for e in evs:
        t = dt(e["ts"])
        if e["tipo"] == "despertou" and ini:
            out.append((ini, t))
            ini = None
        elif e["tipo"] == "voltou" and ini is None:
            ini = t
    if ini:
        out.append((ini, fim))
    return [(a, b) for a, b in out if b > a]


def resumo(s, evs):
    bl = blocos(s, evs)
    total = sum((b - a for a, b in bl), timedelta())
    ini, fim = dt(s["adormeceu"]), dt(s["acordou"]) or agora()
    lat = None
    if ini and s["inicio"] and not s["estimado"] and ini > dt(s["inicio"]):
        lat = (ini - dt(s["inicio"])).total_seconds() / 60
    return {
        "blocos": bl,
        "total": total,
        "acordada": (fim - ini - total) if ini else timedelta(),
        "despertares": sum(e["tipo"] == "despertou" for e in evs),
        "mamadas": sum(e["tipo"] == "mamada" for e in evs),
        "maior": max((b - a for a, b in bl), default=timedelta()),
        "latencia": lat,
    }


def sessoes_do_dia(c, ref):
    rows = c.execute(f"SELECT * FROM sessoes WHERE adormeceu IS NOT NULL AND status IN ('fim',"
                     f"'dormindo','despertou') ORDER BY adormeceu").fetchall()
    return [s for s in rows if dia_ref(dt(s["adormeceu"])) == ref]


def total_do_dia(c, ref):
    return sum((resumo(s, eventos(c, s["id"]))["total"] for s in sessoes_do_dia(c, ref)), timedelta())


# ───────────────────────── Ações ─────────────────────────
def a_dormir(c, ts, chat_id, args, jq, tipo=None):
    s = sessao_aberta(c)
    if s and s["status"] != "tentando":
        return f"{NOME} já tá registrada dormindo desde {hm(dt(s['adormeceu']))}. Se acordou, manda /acordou."
    if s:  # nova tentativa: descarta a anterior
        cancela_lembretes(jq, s["id"])
        c.execute("UPDATE sessoes SET status='cancelada' WHERE id=?", (s["id"],))
    t2, local = extrai_tipo_local(args)
    tipo = tipo or t2 or tipo_auto(ts)
    sid = c.execute("INSERT INTO sessoes(tipo,local,inicio,status,chat_id) VALUES(?,?,?,'tentando',?)",
                    (tipo, local, ts.isoformat(), chat_id)).lastrowid
    agenda_lembretes(jq, sid, ts, chat_id)

    txt = frase("dormir", h=hm(ts))
    txt += f"\n{'🛏️ Noite' if tipo == 'noite' else '💤 Soneca'}" + (f" · {local}" if local else "")
    ult = ultima_fechada(c)
    if ult and ts - dt(ult["acordou"]) < timedelta(hours=12):
        txt += f"\n⏳ Janela acordada: {dur(ts - dt(ult['acordou']))}"
    txt += f"\nTe aviso em {', '.join(map(str, LEMBRETES))} min."
    return txt


def a_dormiu(c, ts, chat_id, args, jq):
    s = sessao_aberta(c)
    if not s:  # dormiu sem aviso (carro, colo...)
        t2, local = extrai_tipo_local(args)
        tipo = t2 or tipo_auto(ts)
        c.execute("INSERT INTO sessoes(tipo,local,inicio,adormeceu,status,chat_id) VALUES(?,?,?,?,'dormindo',?)",
                  (tipo, local, ts.isoformat(), ts.isoformat(), chat_id))
        return frase("dormiu_surpresa", h=hm(ts), det=tipo + (f", {local}" if local else ""))
    if s["status"] == "despertou":
        return a_voltou(c, ts, chat_id, args, jq)
    if s["status"] == "dormindo":
        return "Ela já tá marcada como dormindo 😉 Relaxa que tá anotado."
    cancela_lembretes(jq, s["id"])
    ini = dt(s["inicio"])
    ts = max(ts, ini)
    _, local = extrai_tipo_local(args)
    c.execute("UPDATE sessoes SET adormeceu=?, status='dormindo', local=COALESCE(?, local) WHERE id=?",
              (ts.isoformat(), local, s["id"]))
    return frase("dormiu", h=hm(ts), lat=dur(ts - ini))


def a_despertou(c, ts, chat_id, args, jq):
    s = sessao_aberta(c)
    if not s or s["status"] == "tentando":
        return "Ela ainda não tava marcada dormindo. Usa /dormiu primeiro (ou /cancelar)."
    if s["status"] == "despertou":
        return "Já tá marcado que ela despertou. Quando voltar a dormir: /voltou"
    evs = eventos(c, s["id"])
    bl = blocos(s, evs)
    inicio_bloco = bl[-1][0] if bl else dt(s["adormeceu"])
    add_evento(c, s["id"], "despertou", ts)
    c.execute("UPDATE sessoes SET status='despertou' WHERE id=?", (s["id"],))
    return frase("despertou", h=hm(ts), b=dur(ts - inicio_bloco))


def a_voltou(c, ts, chat_id, args, jq):
    s = sessao_aberta(c)
    if not s or s["status"] != "despertou":
        return "Não tem despertar aberto. Se ela dormiu agora, usa /dormiu."
    ultimo = [e for e in eventos(c, s["id"]) if e["tipo"] == "despertou"][-1]
    add_evento(c, s["id"], "voltou", ts)
    c.execute("UPDATE sessoes SET status='dormindo' WHERE id=?", (s["id"],))
    return frase("voltou", h=hm(ts), a=dur(ts - dt(ultimo["ts"])))


def a_acordou(c, ts, chat_id, args, jq, explicito=False):
    s = sessao_aberta(c)
    if not s:
        return "Não tem sono aberto. Manda /dormir (ou /dormiu se ela já apagou)."
    cancela_lembretes(jq, s["id"])
    if s["status"] == "tentando":
        ad = dt(s["inicio"]) + timedelta(minutes=LATENCIA_PADRAO)
        if ad >= ts:
            c.execute("UPDATE sessoes SET status='cancelada' WHERE id=?", (s["id"],))
            return frase("nem_dormiu")
        c.execute("UPDATE sessoes SET adormeceu=?, estimado=1 WHERE id=?", (ad.isoformat(), s["id"]))
    if s["status"] == "despertou" and not explicito:
        ts = dt([e for e in eventos(c, s["id"]) if e["tipo"] == "despertou"][-1]["ts"])
    c.execute("UPDATE sessoes SET acordou=?, status='fim' WHERE id=?", (ts.isoformat(), s["id"]))

    s = c.execute("SELECT * FROM sessoes WHERE id=?", (s["id"],)).fetchone()
    r = resumo(s, eventos(c, s["id"]))
    ad = dt(s["adormeceu"])
    linhas = [
        frase("acordou", h=hm(ts)),
        f"{'🛏️ Noite' if s['tipo'] == 'noite' else '💤 Soneca'}" + (f" · {s['local']}" if s["local"] else ""),
        f"😴 Dormiu {dur(r['total'])} ({hm(ad)}{'*' if s['estimado'] else ''} → {hm(ts)})",
    ]
    if r["despertares"]:
        linhas.append(f"🌙 {r['despertares']} despertar(es) · {dur(r['acordada'])} acordada")
        linhas.append(f"🏆 Maior sono seguido: {dur(r['maior'])}")
    linhas.append(f"📅 Total do dia até agora: {dur(total_do_dia(c, dia_ref(ad)))}")
    if s["tipo"] == "noite" and r["total"] >= timedelta(hours=3):
        if r["despertares"] == 0:
            linhas.append(frase("sem_despertar"))
        elif r["despertares"] >= 3:
            linhas.append(frase("muitos_despertares"))
    if s["estimado"]:
        linhas.append(f"* horário estimado (início + {LATENCIA_PADRAO} min)")
    return "\n".join(linhas)


def a_mamada(c, ts, chat_id, args, jq):
    s = sessao_aberta(c)
    obs = " ".join(args) or None
    add_evento(c, s["id"] if s else None, "mamada", ts, obs)
    return f"🍼 Mamada às {hm(ts)} anotada" + (f" ({obs})" if obs else "") + "."


def a_nota(c, texto):
    if not texto:
        return "Manda assim: /nota tá com o dente nascendo"
    s = sessao_aberta(c)
    add_evento(c, s["id"] if s else None, "nota", agora(), texto)
    return "📝 Nota salva."


def a_cancelar(c, jq):
    s = sessao_aberta(c)
    if not s:
        return "Não tem nada aberto pra cancelar."
    cancela_lembretes(jq, s["id"])
    c.execute("UPDATE sessoes SET status='cancelada' WHERE id=?", (s["id"],))
    return frase("cancelar")


def a_desfazer(c, jq):
    s = c.execute("SELECT * FROM sessoes ORDER BY id DESC LIMIT 1").fetchone()
    if not s:
        return "Nada pra desfazer."
    sid = s["id"]
    if s["status"] == "cancelada":
        st = "tentando" if not s["adormeceu"] else "dormindo"
        c.execute("UPDATE sessoes SET status=? WHERE id=?", (st, sid))
        return "↩️ Desfeito: o cancelamento foi revertido."
    if s["status"] == "fim":
        if s["estimado"]:
            c.execute("UPDATE sessoes SET acordou=NULL, adormeceu=NULL, estimado=0, status='tentando' WHERE id=?",
                      (sid,))
        else:
            evs = [e for e in eventos(c, sid) if e["tipo"] in ("despertou", "voltou")]
            st = "despertou" if evs and evs[-1]["tipo"] == "despertou" else "dormindo"
            c.execute("UPDATE sessoes SET acordou=NULL, status=? WHERE id=?", (st, sid))
        return "↩️ Desfeito: ela voltou a constar como dormindo."
    if s["status"] == "tentando":
        cancela_lembretes(jq, sid)
        c.execute("DELETE FROM sessoes WHERE id=?", (sid,))
        return "↩️ Desfeito: tentativa apagada."
    ev = c.execute("SELECT * FROM eventos WHERE sessao_id=? AND tipo IN ('despertou','voltou') "
                   "ORDER BY ts DESC, id DESC LIMIT 1", (sid,)).fetchone()
    if ev:
        c.execute("DELETE FROM eventos WHERE id=?", (ev["id"],))
        st = "dormindo" if ev["tipo"] == "despertou" else "despertou"
        c.execute("UPDATE sessoes SET status=? WHERE id=?", (st, sid))
        return f"↩️ Desfeito: '{ev['tipo']}' das {hm(dt(ev['ts']))}."
    if s["inicio"] == s["adormeceu"]:
        c.execute("DELETE FROM sessoes WHERE id=?", (sid,))
        return "↩️ Desfeito: sono apagado."
    c.execute("UPDATE sessoes SET adormeceu=NULL, status='tentando' WHERE id=?", (sid,))
    return "↩️ Desfeito: voltou pra 'tentando dormir'."


def a_status(c):
    s, t = sessao_aberta(c), agora()
    if not s:
        u = ultima_fechada(c)
        return f"☀️ Acordada desde {hm(dt(u['acordou']))} (há {dur(t - dt(u['acordou']))})." if u \
            else "Nada registrado ainda. Manda /dormir quando for colocar ela pra dormir."
    if s["status"] == "tentando":
        return f"⏳ Tentando dormir desde {hm(dt(s['inicio']))} (há {dur(t - dt(s['inicio']))})."
    r = resumo(s, eventos(c, s["id"]))
    if s["status"] == "despertou":
        d = [e for e in eventos(c, s["id"]) if e["tipo"] == "despertou"][-1]
        return f"🌙 Despertou às {hm(dt(d['ts']))} (há {dur(t - dt(d['ts']))}). Dormiu {dur(r['total'])} até agora."
    return (f"😴 Dormindo desde {hm(dt(s['adormeceu']))}. Bloco atual: {dur(t - r['blocos'][-1][0])} · "
            f"total: {dur(r['total'])}.")


def a_hoje(c):
    ref = dia_ref(agora())
    ss = sessoes_do_dia(c, ref)
    if not ss:
        return "Nenhum sono registrado hoje ainda."
    linhas, tot = [f"📅 Hoje ({ref:%d/%m}):"], timedelta()
    for s in ss:
        r = resumo(s, eventos(c, s["id"]))
        tot += r["total"]
        fim = hm(dt(s["acordou"])) if s["acordou"] else "agora"
        extra = f" · {r['despertares']}🌙" if r["despertares"] else ""
        linhas.append(f"{'🛏️' if s['tipo'] == 'noite' else '💤'} {hm(dt(s['adormeceu']))}–{fim}: "
                      f"{dur(r['total'])}{extra}")
    linhas.append(f"Total: {dur(tot)}")
    return "\n".join(linhas)


# ───────────────────────── Lembretes ─────────────────────────
def agenda_lembretes(jq, sid, inicio, chat_id):
    if jq is None:
        return
    for i, m in enumerate(LEMBRETES):
        quando = inicio + timedelta(minutes=m)
        if quando > agora():
            jq.run_once(lembrete, when=quando, name=f"s{sid}",
                        data={"sid": sid, "chat_id": chat_id, "i": i, "min": m})


def cancela_lembretes(jq, sid):
    if jq is None:
        return
    for job in jq.get_jobs_by_name(f"s{sid}"):
        job.schedule_removal()


async def lembrete(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    with db() as c:
        s = c.execute("SELECT status FROM sessoes WHERE id=?", (d["sid"],)).fetchone()
    if not s or s["status"] != "tentando":
        return
    txt = texto_lembrete(d["i"], d["min"])
    await context.bot.send_message(d["chat_id"], txt, reply_markup=teclado("tentando"))


def texto_lembrete(i, m):
    chave = "l_primeiro" if i == 0 else ("l_meio" if i < len(LEMBRETES) - 1 else "l_ultimo")
    return frase(chave, m=m)


def recarrega_lembretes(jq):
    """Depois de reiniciar o bot, reagenda o que ainda falta."""
    with db() as c:
        for s in c.execute("SELECT * FROM sessoes WHERE status='tentando'").fetchall():
            agenda_lembretes(jq, s["id"], dt(s["inicio"]), s["chat_id"])


# ───────────────────────── Relatório semanal ─────────────────────────
DIAS_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def rotulo(d):
    return f"{DIAS_PT[d.weekday()]} {d:%d/%m}"


def estatisticas(c, d_ini, d_fim):
    dias = [d_ini + timedelta(days=i) for i in range((d_fim - d_ini).days + 1)]
    por = {d: dict(noite=0.0, soneca=0.0, n_soneca=0, despertares=0, acordada=0.0, lat=[],
                   bedtime=None, maior=0.0) for d in dias}
    todos_blocos, mamadas = [], []
    rows = c.execute("SELECT * FROM sessoes WHERE adormeceu IS NOT NULL "
                     "AND status IN ('fim','dormindo','despertou')").fetchall()
    for s in rows:
        evs = eventos(c, s["id"])
        r = resumo(s, evs)
        todos_blocos += [(a, b, s["tipo"]) for a, b in r["blocos"]]
        mamadas += [dt(e["ts"]) for e in evs if e["tipo"] == "mamada"]
        ref = dia_ref(dt(s["adormeceu"]))
        if ref not in por:
            continue
        p = por[ref]
        p[s["tipo"]] += r["total"].total_seconds() / 3600
        if r["latencia"] is not None:
            p["lat"].append(r["latencia"])
        if s["tipo"] == "soneca":
            p["n_soneca"] += 1
        else:
            p["despertares"] += r["despertares"]
            p["acordada"] += r["acordada"].total_seconds() / 60
            p["maior"] = max(p["maior"], r["maior"].total_seconds() / 3600)
            b = dt(s["adormeceu"])
            if p["bedtime"] is None or b < p["bedtime"]:
                p["bedtime"] = b
    mamadas += [dt(e["ts"]) for e in c.execute("SELECT ts FROM eventos WHERE tipo='mamada' AND sessao_id IS NULL")]
    return dias, por, todos_blocos, mamadas


def hora_dec(d):
    h = d.hour + d.minute / 60
    return h + 24 if h < 12 else h  # 01h vira 25 pra não quebrar o gráfico


def grafico(dias, por, todos_blocos, mamadas):
    COR_N, COR_S = "#3b4cc0", "#f4a259"
    labels, x = [rotulo(d) for d in dias], list(range(len(dias)))
    fig = plt.figure(figsize=(11, 13))
    gs = fig.add_gridspec(3, 2, height_ratios=[1.4, 1, 1], hspace=0.5, wspace=0.28)
    fig.suptitle(f"Sono da {NOME} · {rotulo(dias[0])} → {rotulo(dias[-1])}", fontsize=15, fontweight="bold")

    # 1) Linha do tempo 06h → 06h
    ax = fig.add_subplot(gs[0, :])
    for i, d in enumerate(dias):
        base = datetime.combine(d, dtime(VIRADA_DO_DIA, 0), TZ)
        topo = base + timedelta(days=1)
        for a, b, tipo in todos_blocos:
            a2, b2 = max(a, base), min(b, topo)
            if b2 > a2:
                ax.broken_barh([((a2 - base).total_seconds() / 3600, (b2 - a2).total_seconds() / 3600)],
                               (i - 0.35, 0.7), color=COR_N if tipo == "noite" else COR_S)
    ax.set_yticks(x, labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 2), [f"{(VIRADA_DO_DIA + h) % 24:02d}h" for h in range(0, 25, 2)])
    ax.grid(axis="x", alpha=0.3)
    ax.set_title("Linha do tempo")
    ax.legend(handles=[Patch(color=COR_N, label="noite"), Patch(color=COR_S, label="soneca")],
              loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=2, frameon=False)

    # 2) Total por dia
    ax = fig.add_subplot(gs[1, 0])
    n = [por[d]["noite"] for d in dias]
    s = [por[d]["soneca"] for d in dias]
    ax.bar(x, n, color=COR_N, label="noite")
    ax.bar(x, s, bottom=n, color=COR_S, label="soneca")
    for i, (a, b) in enumerate(zip(n, s)):
        if a + b:
            ax.text(i, a + b + 0.2, fh(a + b), ha="center", fontsize=8)
    ax.set_xticks(x, [l.split()[0] for l in labels])
    ax.set_ylabel("horas")
    ax.set_title("Total de sono por dia")
    ax.set_ylim(0, max([a + b for a, b in zip(n, s)] + [1]) * 1.18)

    # 3) Despertares
    ax = fig.add_subplot(gs[1, 1])
    dsp = [por[d]["despertares"] for d in dias]
    ax.bar(x, dsp, color="#6c757d")
    for i, d in enumerate(dias):
        if por[d]["acordada"]:
            ax.text(i, dsp[i] + 0.1, f"{int(por[d]['acordada'])}m", ha="center", fontsize=8)
    ax.set_xticks(x, [l.split()[0] for l in labels])
    ax.set_title("Despertares à noite (min acordada)")
    ax.set_ylim(0, max(dsp + [1]) + 1)
    ax.yaxis.get_major_locator().set_params(integer=True)

    # 4) Hora que dormiu à noite
    ax = fig.add_subplot(gs[2, 0])
    pts = [(i, hora_dec(por[d]["bedtime"])) for i, d in enumerate(dias) if por[d]["bedtime"]]
    if pts:
        ax.plot([p[0] for p in pts], [p[1] for p in pts], "o-", color=COR_N)
        lo, hi = int(min(p[1] for p in pts)) - 1, int(max(p[1] for p in pts)) + 2
        ax.set_ylim(lo, hi)
        ax.set_yticks(range(lo, hi + 1), [f"{h % 24:02d}h" for h in range(lo, hi + 1)])
    ax.set_xticks(x, [l.split()[0] for l in labels])
    ax.set_xlim(-0.5, len(dias) - 0.5)
    ax.grid(alpha=0.3)
    ax.set_title("Hora que dormiu (noite)")

    # 5) Tempo pra pegar no sono
    ax = fig.add_subplot(gs[2, 1])
    lat = [mean(por[d]["lat"]) if por[d]["lat"] else 0 for d in dias]
    ax.bar(x, lat, color="#8ab17d")
    ax.axhline(LATENCIA_PADRAO, color="gray", ls="--", lw=1)
    ax.set_xticks(x, [l.split()[0] for l in labels])
    ax.set_ylabel("min")
    ax.set_title("Tempo pra pegar no sono (média)")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def texto_semana(dias, por, prev):
    com = [d for d in dias if por[d]["noite"] + por[d]["soneca"] > 0]
    noites = [d for d in dias if por[d]["noite"] > 0]
    tot = mean(por[d]["noite"] + por[d]["soneca"] for d in com)
    linhas = [f"📊 Boletim semanal da {NOME} ({len(com)} dias com registro)",
              f"😴 Média total: {fh(tot)}/dia"]
    prev_com = [v for v in prev.values() if v["noite"] + v["soneca"] > 0]
    if prev_com:
        diff = tot - mean(v["noite"] + v["soneca"] for v in prev_com)
        linhas[-1] += f" ({'+' if diff >= 0 else '−'}{fh(abs(diff))} vs semana passada)"
    if noites:
        linhas.append(f"🛏️ Noite: {fh(mean(por[d]['noite'] for d in noites))} · "
                      f"{mean(por[d]['despertares'] for d in noites):.1f} despertares/noite")
        melhor = max(noites, key=lambda d: por[d]["maior"])
        linhas.append(f"🏆 Maior sono seguido: {fh(por[melhor]['maior'])} ({rotulo(melhor)})")
        bts = [hora_dec(por[d]["bedtime"]) for d in noites if por[d]["bedtime"]]
        if bts:
            m = mean(bts) % 24
            linhas.append(f"🕗 Hora média de dormir: {int(m):02d}:{int(m % 1 * 60):02d}")
    linhas.append(f"💤 Sonecas: {mean(por[d]['n_soneca'] for d in com):.1f}/dia · "
                  f"{fh(mean(por[d]['soneca'] for d in com))}/dia")
    lats = [v for d in dias for v in por[d]["lat"]]
    if lats:
        linhas.append(f"⏱️ Pra pegar no sono: {int(mean(lats))} min em média")
    return "\n".join(linhas)


async def envia_relatorio(context, chat_id, d_ini, d_fim):
    with db() as c:
        dias, por, bl, mam = estatisticas(c, d_ini, d_fim)
        _, prev, _, _ = estatisticas(c, d_ini - timedelta(days=7), d_fim - timedelta(days=7))
    if not any(por[d]["noite"] + por[d]["soneca"] for d in dias):
        await context.bot.send_message(chat_id, "Ainda não tenho sono registrado nesse período 🤷")
        return
    await context.bot.send_photo(chat_id, photo=grafico(dias, por, bl, mam))
    await context.bot.send_message(chat_id, texto_semana(dias, por, prev))


async def relatorio_auto(context: ContextTypes.DEFAULT_TYPE):
    fim = dia_ref(agora()) - timedelta(days=1)  # segunda de manhã: fecha seg→dom (com a noite de domingo)
    with db() as c:
        chats = [r["chat_id"] for r in c.execute("SELECT chat_id FROM chats")]
    for ch in chats:
        await envia_relatorio(context, ch, fim - timedelta(days=6), fim)


# ───────────────────────── Telegram ─────────────────────────
def teclado(status):
    B = InlineKeyboardButton
    if status == "tentando":
        rows = [[B("😴 Dormiu", callback_data="dormiu"), B("❌ Desistiu", callback_data="cancelar")]]
    elif status == "dormindo":
        rows = [[B("🌙 Despertou", callback_data="despertou"), B("☀️ Acordou de vez", callback_data="acordou")]]
    elif status == "despertou":
        rows = [[B("😴 Voltou a dormir", callback_data="voltou"), B("☀️ Acordou de vez", callback_data="acordou")]]
    else:
        rows = [[B("🛏️ Noite", callback_data="noite"), B("💤 Soneca", callback_data="soneca")]]
    return InlineKeyboardMarkup(rows)


PALAVRAS = {
    "dormir": "dormir", "noite": "noite", "soneca": "soneca", "cochilo": "soneca",
    "dormiu": "dormiu", "apagou": "dormiu", "pegou": "dormiu",
    "despertou": "despertou", "chorou": "despertou",
    "voltou": "voltou",
    "acordou": "acordou",
    "cancela": "cancelar", "cancelar": "cancelar", "desistiu": "cancelar", "desisti": "cancelar",
    "desfazer": "desfazer", "desfaz": "desfazer", "ops": "desfazer",
    "status": "status", "hoje": "hoje", "semana": "semana",
}
MUDAM_ESTADO = {"dormir", "noite", "soneca", "dormiu", "despertou", "voltou", "acordou",
                "cancelar", "desfazer"}


async def checa(update: Update):
    uid = update.effective_user.id
    if uid in ALLOWED:
        with db() as c:
            c.execute("INSERT OR IGNORE INTO chats VALUES(?)", (update.effective_chat.id,))
        return True
    msg = f"Sem permissão. Teu ID é {uid} — coloca ele em ALLOWED_USERS."
    if update.callback_query:
        await update.callback_query.answer(msg, show_alert=True)
    else:
        await update.effective_message.reply_text(msg)
    return False


async def executar(update: Update, context: ContextTypes.DEFAULT_TYPE, acao, args):
    if not await checa(update):
        return
    chat_id, jq = update.effective_chat.id, context.job_queue
    if acao == "semana":
        hoje = dia_ref(agora())
        await envia_relatorio(context, chat_id, hoje - timedelta(days=6), hoje)
        return
    ts, resto, explicito = parse_hora(args)
    with db() as c:
        if acao in ("dormir", "noite", "soneca"):
            txt = a_dormir(c, ts, chat_id, resto, jq, None if acao == "dormir" else acao)
        elif acao == "dormiu":
            txt = a_dormiu(c, ts, chat_id, resto, jq)
        elif acao == "despertou":
            txt = a_despertou(c, ts, chat_id, resto, jq)
        elif acao == "voltou":
            txt = a_voltou(c, ts, chat_id, resto, jq)
        elif acao == "acordou":
            txt = a_acordou(c, ts, chat_id, resto, jq, explicito)
        elif acao == "nota":
            txt = a_nota(c, " ".join(args))
        elif acao == "cancelar":
            txt = a_cancelar(c, jq)
        elif acao == "desfazer":
            txt = a_desfazer(c, jq)
        elif acao == "status":
            txt = a_status(c)
        elif acao == "hoje":
            txt = a_hoje(c)
        else:
            txt = AJUDA
        status = status_atual(c)
    await context.bot.send_message(chat_id, txt, reply_markup=teclado(status))

    if AVISAR_OUTROS and acao in MUDAM_ESTADO:
        with db() as c:
            outros = [r["chat_id"] for r in c.execute("SELECT chat_id FROM chats WHERE chat_id != ?", (chat_id,))]
        for o in outros:
            try:
                await context.bot.send_message(o, f"👤 {update.effective_user.first_name}: {txt}",
                                               reply_markup=teclado(status))
            except Exception as e:  # noqa: BLE001
                log.warning("Falha avisando %s: %s", o, e)


def comando(acao):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await executar(update, context, acao, context.args or [])
    return handler


async def botao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_reply_markup(None)  # some com os botões velhos
    except Exception:  # noqa: BLE001
        pass
    await executar(update, context, q.data, [])


async def texto_livre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    palavras = update.message.text.split()
    for i, p in enumerate(palavras):
        if norm(p) in PALAVRAS:
            await executar(update, context, PALAVRAS[norm(p)], palavras[i + 1:] + palavras[:i])
            return
    await executar(update, context, "nota", palavras)  # sem palavra-chave = nota


AJUDA = f"""👶 Bot do sono da {NOME}

/dormir [noite|soneca] [local] — tô indo colocar ela pra dormir
/dormiu — pegou no sono (sem /dormir antes = apagou do nada)
/despertou · /voltou — despertares no meio do sono
/acordou — acordou de vez, te mando o resumo
/nota texto — anotação (dente, vacina...)
/cancelar · /desfazer
/status · /hoje · /semana

⏰ Qualquer um aceita hora: "acordou 06:40", "dormiu -15" (15 min atrás)
💬 Pode escrever sem barra: "ela dormiu no carro"
📍 Locais: {', '.join(sorted(set(LOCAIS.values())))}"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"{AJUDA}\n\nTeu ID: {update.effective_user.id}")


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("dormir", "Vou colocar pra dormir"), BotCommand("dormiu", "Pegou no sono"),
        BotCommand("despertou", "Despertou no meio"), BotCommand("voltou", "Voltou a dormir"),
        BotCommand("acordou", "Acordou de vez"), BotCommand("soneca", "Começar soneca"),
        BotCommand("nota", "Anotação"),
        BotCommand("status", "Como tá agora"), BotCommand("hoje", "Resumo de hoje"),
        BotCommand("semana", "Gráficos da semana"), BotCommand("desfazer", "Desfazer última"),
        BotCommand("cancelar", "Cancelar"), BotCommand("ajuda", "Ajuda"),
    ])


def main():
    if not TOKEN:
        raise SystemExit("Defina TELEGRAM_TOKEN")
    if not ALLOWED:
        log.warning("ALLOWED_USERS vazio: o bot só vai responder o /start com o teu ID.")
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ajuda", comando("ajuda")))
    for cmd in ["dormir", "noite", "soneca", "dormiu", "despertou", "voltou", "acordou",
                "nota", "cancelar", "desfazer", "status", "hoje", "semana"]:
        app.add_handler(CommandHandler(cmd, comando(cmd)))
    app.add_handler(CallbackQueryHandler(botao))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto_livre))

    recarrega_lembretes(app.job_queue)
    # segunda 08h: relatório da semana que passou (0=domingo … 6=sábado no python-telegram-bot)
    app.job_queue.run_daily(relatorio_auto, time=dtime(8, 0, tzinfo=TZ), days=(1,))
    app.run_polling()


if __name__ == "__main__":
    main()
