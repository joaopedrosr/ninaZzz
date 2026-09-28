"""
Versão webhook do bot, pra rodar grátis no PythonAnywhere.
Reaproveita toda a lógica do bot.py; só troca o "motor":
  - o Telegram chama /hook/<SEGREDO> a cada mensagem
  - o cron-job.org chama /tick/<SEGREDO> a cada minuto (lembretes + relatório de segunda)
  - abrir /setup/<SEGREDO> uma vez no navegador liga o webhook
"""
import json
import os
import random
from datetime import date, timedelta

import requests
from flask import Flask, abort, request

import bot as core

SEGREDO = os.environ.get("SEGREDO", "")
NASCIMENTO = os.environ.get("NASCIMENTO", "")  # "AAAA-MM-DD" — sem isso, sem avisos de janela

# Janela acordada de referência por idade: (até N meses completos, mínimo, máximo) em minutos.
# São valores gerais usados em guias de sono infantil — ajuste se o pediatra orientar diferente.
JANELAS = [
    (1, 35, 60),     # 0 meses
    (2, 60, 90),     # 1 mês
    (3, 75, 105),    # 2 meses
    (4, 90, 120),    # 3 meses
    (6, 120, 150),   # 4–5 meses
    (8, 150, 180),   # 6–7 meses
    (10, 180, 210),  # 8–9 meses
    (12, 180, 240),  # 10–11 meses
    (15, 210, 270),  # 12–14 meses
    (18, 240, 330),  # 15–17 meses
    (24, 300, 360),  # 18–23 meses
]
API = f"https://api.telegram.org/bot{core.TOKEN}"
app = Flask(__name__)


# ───────────── lembretes guardados no banco (em vez do JobQueue) ─────────────
def agenda(c, sid, inicio, chat_id):
    for i, m in enumerate(core.LEMBRETES):
        quando = inicio + timedelta(minutes=m)
        if quando > core.agora():
            c.execute("INSERT INTO lembretes(sid,chat_id,i,min,due,tipo) VALUES(?,?,?,?,?,'sono')",
                      (sid, chat_id, i, m, quando.isoformat()))


def cancela(c, sid):
    c.execute("DELETE FROM lembretes WHERE sid=? AND tipo='sono'", (sid,))


core.agenda_lembretes = agenda   # o bot.py passa a usar essas duas
core.cancela_lembretes = cancela


JANELA_INI = [
    "⏳ {n} tá acordada há {a}. A janela abriu! Olho nos sinais: bocejo, olhar de zumbi, coçar os olhos 👀",
    "⏳ {a} acordada. Os primeiros bocejos vêm aí... vai preparando o terreno 🛏️",
    "⏳ {a} de festa. Daqui a pouco o DJ encerra a balada 🎧",
]
JANELA_FIM = [
    "⏰ {a} acordada! Limite da janela. Passou daqui, ela vira um gremlin 👹 Hora de dormir!",
    "⏰ Alerta vermelho: {a} acordada. Bora pra cama antes do modo birra 😤",
    "⏰ {n} já tá há {a} acordada. Última chamada pro voo Nana, embarque imediato ✈️",
]


JANELA_INI += [
    "⏳ {a} acordada. Segundo tempo começando, aquece o banco de reservas 🪑",
    "⏳ Coé! {a} de pé, mermão. Já pode ir preparando o berço 🛏️",
    "⏳ {a} acordada. Hora de ir pensando en la siesta 🇪🇸",
    "⏳ {n} tá há {a} no rolê. O sono vem que nem saudade: devagar e certeiro 🇧🇷",
]
JANELA_FIM += [
    "⏰ {a} acordada! Acréscimos esgotados, o juiz vai apitar! 🟥",
    "⏰ Mermão, {a} acordada! Papo reto: cama AGORA 🏃",
    "⏰ ¡Ya está! {a} despierta. A la cuna, que viene la rabieta 🇪🇸",
    "⏰ {a} de jogo! Até o Mengão pede substituição nessa hora 🔴⚫",
]


JANELA_INI += [
    "⏳ {a} acordada. Hora de ir baixando o volume da roda de samba 🥁",
    "⏳ {a} de pé. Clima de 'Sonho Meu' chegando, fica de olho nos bocejos 🌙",
    "⏳ {a} acordada. Paulinho da Viola já tá afinando o cavaco pra ninar 🎸",
]
JANELA_FIM += [
    "⏰ {a} acordada! 'O Show Tem que Continuar'... mas no berço! 🎤",
    "⏰ {a} de pé! Vai virar 'Juízo Final' se passar daqui 😅 Cama já!",
    "⏰ {a} acordada. Última chamada pro 'Trem das Onze', destino berço 🚂",
]

core.FRASES.update({
    "proxima_janela": [
        "⏳ Próxima janela ({i}): entre {a} e {b}. Te aviso nos dois horários.",
        "⏳ Janela ({i}) vai de {a} a {b}. Eu apito no começo e no fim 🟨",
        "⏳ Anota aí ({i}): sono entre {a} e {b}. Te cutuco nos dois 👉",
        "⏳ Próxima partida ({i}): entre {a} e {b} ⚽ Te aviso!",
        "⏳ Roda de samba liberada até {a}–{b} ({i}) 🥁 Te aviso nos dois.",
    ],
    "janela_info": [
        "⏳ Janela ideal ({i}): {lo}–{hi} min → {a} a {b}",
        "⏳ Pra {i}: {lo}–{hi} min acordada → dormir entre {a} e {b}",
        "⏳ Janela ({i}): {a} até {b} ({lo}–{hi} min) 🎯",
    ],
    "jan_sem_nasc": [
        "Preciso da data de nascimento dela (NASCIMENTO no WSGI) pra calcular a janela 🎂",
        "Sem a data de nascimento no WSGI não consigo calcular, mermão 🎂",
    ],
    "jan_tentando": [
        "⏳ Tá rolando uma tentativa desde {h}. Foco na missão! 🥷",
        "⏳ Bola rolando desde {h}, a janela fica pra depois ⚽",
        "⏳ Ninando desde {h}. Agora é com você, mermão 💪",
        "⏳ Roda de ninar desde {h}. Canta baixinho 🎶",
    ],
    "jan_dormindo": [
        "😴 Ela tá dormindo desde {h}. A janela só conta quando ela acordar. Aproveita e descansa também 🛋️",
        "😴 Dormindo desde {h}! Janela pausada. Vai tirar um cochilo também, mermão 😎",
        "😴 Em 'Sonho Meu' desde {h}. A janela começa quando ela acordar 🌙",
        "😴 Siesta en curso desde {h}. La ventana empieza cuando despierte 🇪🇸",
    ],
    "jan_sem_dados": [
        "Ainda não tenho nenhum 'acordou' registrado pra calcular a janela 🤷",
        "Sem nenhum 'acordou' no registro ainda, mermão 🤷",
    ],
    "jan_cab": [
        "⏳ Janela pra {i}: {lo}–{hi} min\n☀️ Acordada desde {h} (há {x})",
        "⏳ Idade: {i} → janela de {lo}–{hi} min\n☀️ De pé desde {h} (há {x})",
        "⏳ Pra {i}, o ideal é {lo}–{hi} min\n☀️ Em campo desde {h} (há {x}) ⚽",
    ],
    "jan_verde": [
        "🟢 Ainda tem {r} de farra (até {f}). A janela abre às {ab}, daí já vale ficar de olho nos bocejos 👀",
        "🟢 Tranquilo, mermão: {r} até o limite ({f}). A janela abre às {ab} 😎",
        "🟢 Tempo de sobra: {r} (até {f}). Às {ab} começa a janela 🎉",
        "🟢 Roda de samba liberada por mais {r} (até {f}). Janela abre às {ab} 🥁",
        "🟢 Primer tiempo: quedan {r} (hasta {f}). La ventana abre a las {ab} 🇪🇸",
    ],
    "jan_amarelo": [
        "🟡 Janela aberta! Restam {r} até o limite ({f}). Bom momento pra começar o ritual 🛏️",
        "🟡 Segundo tempo: faltam {r} (até {f}). Vai preparando o berço ⚽",
        "🟡 Tá na hora, mermão! Restam {r} até {f} 🛏️",
        "🟡 Hora do samba-canção: {r} até o limite ({f}) 🎶",
    ],
    "jan_vermelho": [
        "🔴 Passou {p} do limite ({f}). Risco de gremlin elevado 👹 Cama já!",
        "🔴 Acréscimos estourados: {p} além do limite ({f}) 🟥 Bora dormir!",
        "🔴 Mermão, já passou {p} do limite ({f})! Cama agora 🏃",
        "🔴 {p} além do limite ({f}). Vai virar 'Juízo Final' 😅 Cama!",
        "🔴 ¡Nos pasamos {p}! (límite {f}) A la cuna ya 🇪🇸",
    ],
})


def idade_meses(d):
    n = date.fromisoformat(NASCIMENTO)
    m = (d.year - n.year) * 12 + d.month - n.month - (1 if d.day < n.day else 0)
    return max(0, m)


def idade_txt(m):
    return "menos de 1 mês" if m == 0 else ("1 mês" if m == 1 else f"{m} meses")


def janela_para(d):
    if not NASCIMENTO:
        return None
    m = idade_meses(d)
    for ate, lo, hi in JANELAS:
        if m < ate:
            return m, lo, hi
    return None


def cancela_janela(c):
    c.execute("DELETE FROM lembretes WHERE tipo LIKE 'janela%'")


def agenda_janela(c):
    """Depois do 'acordou de vez': agenda o aviso de início e de fim da janela."""
    cancela_janela(c)
    ult = core.ultima_fechada(c)
    j = janela_para(core.agora()) if ult else None
    if not j:
        return ""
    m, lo, hi = j
    acordou = core.dt(ult["acordou"])
    for tipo, mins in (("janela_ini", lo), ("janela_fim", hi)):
        quando = acordou + timedelta(minutes=mins)
        if quando > core.agora():
            c.execute("INSERT INTO lembretes(sid,chat_id,i,min,due,tipo) VALUES(?,?,?,?,?,?)",
                      (ult["id"], ult["chat_id"], 0, mins, quando.isoformat(), tipo))
    ini, fim = acordou + timedelta(minutes=lo), acordou + timedelta(minutes=hi)
    return "\n" + core.frase("proxima_janela", i=idade_txt(m), a=core.hm(ini), b=core.hm(fim))


def info_janela(c):
    ult = core.ultima_fechada(c)
    j = janela_para(core.agora()) if ult else None
    if not j:
        return ""
    m, lo, hi = j
    a = core.dt(ult["acordou"])
    return "\n" + core.frase("janela_info", i=idade_txt(m), lo=lo, hi=hi,
                              a=core.hm(a + timedelta(minutes=lo)), b=core.hm(a + timedelta(minutes=hi)))


def a_janela(c):
    if not NASCIMENTO:
        return core.frase("jan_sem_nasc")
    s = core.sessao_aberta(c)
    if s and s["status"] == "tentando":
        return core.frase("jan_tentando", h=core.hm(core.dt(s["inicio"])))
    if s:
        return core.frase("jan_dormindo", h=core.hm(core.dt(s["adormeceu"])))
    ult = core.ultima_fechada(c)
    if not ult:
        return core.frase("jan_sem_dados")
    agora = core.agora()
    m, lo, hi = janela_para(agora)
    a = core.dt(ult["acordou"])
    passou = agora - a
    abre, fecha = a + timedelta(minutes=lo), a + timedelta(minutes=hi)
    cab = core.frase("jan_cab", i=idade_txt(m), lo=lo, hi=hi, h=core.hm(a), x=core.dur(passou)) + "\n"
    if agora < abre:
        return cab + core.frase("jan_verde", r=core.dur(fecha - agora), f=core.hm(fecha), ab=core.hm(abre))
    if agora < fecha:
        return cab + core.frase("jan_amarelo", r=core.dur(fecha - agora), f=core.hm(fecha))
    return cab + core.frase("jan_vermelho", p=core.dur(agora - fecha), f=core.hm(fecha))


def init():
    core.init_db()
    with core.db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS lembretes(id INTEGER PRIMARY KEY, sid INTEGER, chat_id INTEGER,
                                             i INTEGER, min INTEGER, due TEXT);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(lembretes)")]
        if "tipo" not in cols:
            c.execute("ALTER TABLE lembretes ADD COLUMN tipo TEXT DEFAULT 'sono'")


init()


# ───────────── Telegram ─────────────
def tg(metodo, **kw):
    try:
        r = requests.post(f"{API}/{metodo}", timeout=20, **kw)
        if not r.ok:
            app.logger.warning("%s: %s", metodo, r.text)
        return r
    except requests.RequestException as e:
        app.logger.warning("%s falhou: %s", metodo, e)


SEM_TECLADO = object()


def enviar(chat, texto, status=SEM_TECLADO):
    data = {"chat_id": chat, "text": texto}
    if status is not SEM_TECLADO:
        data["reply_markup"] = json.dumps(core.teclado(status).to_dict())
    tg("sendMessage", data=data)


def relatorio(chat, d_ini, d_fim, silencioso=False):
    with core.db() as c:
        dias, por, bl, mam = core.estatisticas(c, d_ini, d_fim)
        _, prev, _, _ = core.estatisticas(c, d_ini - timedelta(days=7), d_fim - timedelta(days=7))
    if not any(por[d]["noite"] + por[d]["soneca"] for d in dias):
        if not silencioso:
            enviar(chat, core.frase("semana_vazia"))
        return
    tg("sendPhoto", data={"chat_id": chat},
       files={"photo": ("semana.png", core.grafico(dias, por, bl, mam), "image/png")})
    enviar(chat, core.texto_semana(dias, por, prev))


def executar(acao, args, user, chat):
    with core.db() as c:
        c.execute("INSERT OR IGNORE INTO chats VALUES(?)", (chat,))
    if acao == "semana":
        hoje = core.dia_ref(core.agora())
        relatorio(chat, hoje - timedelta(days=6), hoje)
        return
    ts, resto, explicito = core.parse_hora(args)
    with core.db() as c:
        antes = core.ultima_fechada(c)
        antes = (antes["id"], antes["acordou"]) if antes else None
        if acao in ("dormir", "noite", "soneca", "dormiu", "desfazer"):
            cancela_janela(c)
        if acao in ("dormir", "noite", "soneca"):
            txt = core.a_dormir(c, ts, chat, resto, c, None if acao == "dormir" else acao)
        elif acao == "dormiu":
            txt = core.a_dormiu(c, ts, chat, resto, c)
        elif acao == "despertou":
            txt = core.a_despertou(c, ts, chat, resto, c)
        elif acao == "voltou":
            txt = core.a_voltou(c, ts, chat, resto, c)
        elif acao == "acordou":
            txt = core.a_acordou(c, ts, chat, resto, c, explicito)
        elif acao == "nota":
            txt = core.a_nota(c, " ".join(args))
        elif acao in ("janela", "janelasono"):
            txt = a_janela(c)
        elif acao in ("editar", "editarultimo"):
            acao = "editar"
            txt = core.a_editar(c, args)
        elif acao == "cancelar":
            txt = core.a_cancelar(c, c)
        elif acao == "desfazer":
            txt = core.a_desfazer(c, c)
        elif acao == "status":
            txt = core.a_status(c)
        elif acao == "hoje":
            txt = core.a_hoje(c)
        else:
            txt = core.ajuda()
        status = core.status_atual(c)
        depois = core.ultima_fechada(c)
        depois = (depois["id"], depois["acordou"]) if depois else None
        if acao in ("acordou", "editar") and status is None and depois and depois != antes:
            txt += agenda_janela(c)
        elif acao == "status" and status is None:
            txt += info_janela(c)
        outros = [r["chat_id"] for r in c.execute("SELECT chat_id FROM chats WHERE chat_id != ?", (chat,))]
    enviar(chat, txt, status)
    if core.AVISAR_OUTROS and acao in core.MUDAM_ESTADO:
        for o in outros:
            enviar(o, f"👤 {user.get('first_name', '')}: {txt}", status)


def trata(up):
    if "callback_query" in up:
        q = up["callback_query"]
        tg("answerCallbackQuery", data={"callback_query_id": q["id"]})
        if q["from"]["id"] not in core.ALLOWED:
            return
        chat = q["message"]["chat"]["id"]
        tg("editMessageReplyMarkup", data={"chat_id": chat, "message_id": q["message"]["message_id"]})
        executar(q["data"], [], q["from"], chat)
        return

    m = up.get("message") or {}
    texto = m.get("text")
    if not texto:
        return
    user, chat = m["from"], m["chat"]["id"]
    palavras = texto.split()
    if palavras[0].startswith("/"):
        cmd = palavras[0][1:].split("@")[0].lower()
        if cmd == "start":
            if user["id"] in core.ALLOWED:
                with core.db() as c:
                    c.execute("INSERT OR IGNORE INTO chats VALUES(?)", (chat,))
            enviar(chat, f"{core.ajuda()}\n\nTeu ID: {user['id']}")
            return
        if user["id"] not in core.ALLOWED:
            enviar(chat, f"Sem permissão. Teu ID é {user['id']} — coloca ele em ALLOWED_USERS.")
            return
        executar(cmd, palavras[1:], user, chat)
        return
    if user["id"] not in core.ALLOWED:
        enviar(chat, f"Sem permissão. Teu ID é {user['id']} — coloca ele em ALLOWED_USERS.")
        return
    for i, p in enumerate(palavras):
        acao = core.PALAVRAS.get(core.norm(p))
        if acao:
            executar(acao, palavras[i + 1:] + palavras[:i], user, chat)
            return
    executar("nota", palavras, user, chat)


# ───────────── Rotas ─────────────
def checa_segredo(s):
    if not SEGREDO or s != SEGREDO:
        abort(404)


@app.post("/hook/<s>")
def hook(s):
    checa_segredo(s)
    up = request.get_json(force=True, silent=True) or {}
    uid = up.get("update_id")
    with core.db() as c:  # o Telegram às vezes reenvia a mesma atualização
        if uid is not None and c.execute("SELECT 1 FROM meta WHERE k=?", (f"u{uid}",)).fetchone():
            return "ok"
        c.execute("INSERT OR IGNORE INTO meta VALUES(?, '1')", (f"u{uid}",))
    try:
        trata(up)
    except Exception:  # noqa: BLE001 — nunca devolver erro pro Telegram, senão ele reenvia
        app.logger.exception("erro tratando update")
    return "ok"


@app.get("/tick/<s>")
def tick(s):
    checa_segredo(s)
    agora = core.agora()
    with core.db() as c:
        rows = c.execute("SELECT l.*, s.status FROM lembretes l JOIN sessoes s ON s.id = l.sid").fetchall()
        vencidos = [r for r in rows if core.dt(r["due"]) <= agora]
        for r in vencidos:
            c.execute("DELETE FROM lembretes WHERE id=?", (r["id"],))
        c.execute("DELETE FROM lembretes WHERE tipo='sono' AND sid NOT IN "
                  "(SELECT id FROM sessoes WHERE status='tentando')")
        aberta = core.sessao_aberta(c)
        ult = core.ultima_fechada(c)
        todos_chats = [x["chat_id"] for x in c.execute("SELECT chat_id FROM chats")]
        c.execute("DELETE FROM meta WHERE k LIKE 'u%' AND rowid NOT IN "
                  "(SELECT rowid FROM meta WHERE k LIKE 'u%' ORDER BY rowid DESC LIMIT 200)")
    for r in vencidos:
        if agora - core.dt(r["due"]) > timedelta(minutes=10):
            continue
        if r["tipo"] and r["tipo"].startswith("janela"):
            # só avisa se ela continua acordada desde aquele "acordou"
            if aberta or not ult or ult["id"] != r["sid"]:
                continue
            acordada = core.dur(agora - core.dt(ult["acordou"]))
            txt = random.choice(JANELA_INI if r["tipo"] == "janela_ini" else JANELA_FIM).format(
                n=core.NOME, a=acordada)
            for ch in todos_chats or [r["chat_id"]]:
                enviar(ch, txt, None)
            continue
        if r["status"] != "tentando":
            continue
        txt = core.texto_lembrete(r["i"], r["min"])
        enviar(r["chat_id"], txt, "tentando")

    # relatório: segunda a partir das 08h, uma vez por semana (seg→dom, com a noite de domingo)
    if agora.weekday() == 0 and agora.hour >= 8:
        semana = agora.strftime("%G-%V")
        with core.db() as c:
            ja = c.execute("SELECT v FROM meta WHERE k='relatorio'").fetchone()
            if not ja or ja["v"] != semana:
                c.execute("INSERT OR REPLACE INTO meta VALUES('relatorio', ?)", (semana,))
                chats = [r["chat_id"] for r in c.execute("SELECT chat_id FROM chats")]
            else:
                chats = []
        fim = core.dia_ref(agora) - timedelta(days=1)
        for ch in chats:
            relatorio(ch, fim - timedelta(days=6), fim, silencioso=True)
    return "ok"


@app.get("/setup/<s>")
def setup(s):
    checa_segredo(s)
    url = request.host_url.replace("http://", "https://") + f"hook/{SEGREDO}"
    r1 = tg("setWebhook", data={"url": url, "drop_pending_updates": "true"})
    cmds = [("dormir", "Vou colocar pra dormir"), ("dormiu", "Pegou no sono"), ("despertou", "Despertou no meio"),
            ("voltou", "Voltou a dormir"), ("acordou", "Acordou de vez"), ("soneca", "Começar soneca"),
            ("janelasono", "Quanto tempo ela aguenta acordada"),
            ("editar", "Corrigir último sono"), ("nota", "Anotação"), ("status", "Como tá agora"),
            ("hoje", "Resumo de hoje"), ("semana", "Gráficos da semana"), ("desfazer", "Desfazer última"),
            ("cancelar", "Cancelar"), ("ajuda", "Ajuda")]
    tg("setMyCommands", data={"commands": json.dumps([{"command": c, "description": d} for c, d in cmds])})
    return f"Webhook: {r1.text if r1 is not None else 'falhou'}"


@app.get("/")
def raiz():
    return "Bot do sono no ar 👶"
