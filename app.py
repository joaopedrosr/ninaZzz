"""
Versão webhook do bot, pra rodar grátis no PythonAnywhere.
Reaproveita toda a lógica do bot.py; só troca o "motor":
  - o Telegram chama /hook/<SEGREDO> a cada mensagem
  - o cron-job.org chama /tick/<SEGREDO> a cada minuto (lembretes + relatório de segunda)
  - abrir /setup/<SEGREDO> uma vez no navegador liga o webhook
"""
import json
import os
from datetime import timedelta

import requests
from flask import Flask, abort, request

import bot as core

SEGREDO = os.environ.get("SEGREDO", "")
API = f"https://api.telegram.org/bot{core.TOKEN}"
app = Flask(__name__)


# ───────────── lembretes guardados no banco (em vez do JobQueue) ─────────────
def agenda(c, sid, inicio, chat_id):
    for i, m in enumerate(core.LEMBRETES):
        quando = inicio + timedelta(minutes=m)
        if quando > core.agora():
            c.execute("INSERT INTO lembretes(sid,chat_id,i,min,due) VALUES(?,?,?,?,?)",
                      (sid, chat_id, i, m, quando.isoformat()))


def cancela(c, sid):
    c.execute("DELETE FROM lembretes WHERE sid=?", (sid,))


core.agenda_lembretes = agenda   # o bot.py passa a usar essas duas
core.cancela_lembretes = cancela


def init():
    core.init_db()
    with core.db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS lembretes(id INTEGER PRIMARY KEY, sid INTEGER, chat_id INTEGER,
                                             i INTEGER, min INTEGER, due TEXT);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """)


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
            enviar(chat, "Ainda não tenho sono registrado nesse período 🤷")
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
        elif acao == "mamada":
            txt = core.a_mamada(c, ts, chat, resto, c)
        elif acao == "nota":
            txt = core.a_nota(c, " ".join(args))
        elif acao == "cancelar":
            txt = core.a_cancelar(c, c)
        elif acao == "desfazer":
            txt = core.a_desfazer(c, c)
        elif acao == "status":
            txt = core.a_status(c)
        elif acao == "hoje":
            txt = core.a_hoje(c)
        else:
            txt = core.AJUDA
        status = core.status_atual(c)
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
            enviar(chat, f"{core.AJUDA}\n\nTeu ID: {user['id']}")
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
        c.execute("DELETE FROM lembretes WHERE sid NOT IN (SELECT id FROM sessoes WHERE status='tentando')")
        c.execute("DELETE FROM meta WHERE k LIKE 'u%' AND rowid NOT IN "
                  "(SELECT rowid FROM meta WHERE k LIKE 'u%' ORDER BY rowid DESC LIMIT 200)")
    for r in vencidos:
        if r["status"] != "tentando" or agora - core.dt(r["due"]) > timedelta(minutes=10):
            continue
        if r["i"] == 0:
            txt = f"⏱️ {r['min']} min — {core.NOME} deve estar começando a pegar no sono 🥱"
        elif r["i"] < len(core.LEMBRETES) - 1:
            txt = f"⏱️ {r['min']} min. Já dormiu?"
        else:
            txt = f"⏱️ {r['min']} min. Se ela apagou, marca aí 👇 Se ainda tá acordada, força aí 💪"
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
            ("mamada", "Registrar mamada"), ("nota", "Anotação"), ("status", "Como tá agora"),
            ("hoje", "Resumo de hoje"), ("semana", "Gráficos da semana"), ("desfazer", "Desfazer última"),
            ("cancelar", "Cancelar"), ("ajuda", "Ajuda")]
    tg("setMyCommands", data={"commands": json.dumps([{"command": c, "description": d} for c, d in cmds])})
    return f"Webhook: {r1.text if r1 is not None else 'falhou'}"


@app.get("/")
def raiz():
    return "Bot do sono no ar 👶"
