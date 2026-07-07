import os, uuid, time, requests, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
from supabase import create_client

_log_lock = threading.Lock()

BRT = ZoneInfo('America/Sao_Paulo')

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BUCKET       = "media"

def get_sb():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

scheduler = BackgroundScheduler(timezone='America/Sao_Paulo')
scheduler.start()

MAX_RETRIES  = 5
RETRY_DELAYS = [5, 10, 30, 60, 120]

# ---------- Banco de dados ----------

def load_data():
    try:
        result = get_sb().table("campaigns").select("id, data").execute()
        return [row["data"] for row in result.data]
    except Exception as e:
        print(f"[DB] load_data error: {e}")
        return []

def save_data(campaigns):
    try:
        sb = get_sb()
        new_ids = {c["id"] for c in campaigns}
        existing = sb.table("campaigns").select("id").execute()
        for row in existing.data:
            if row["id"] not in new_ids:
                sb.table("campaigns").delete().eq("id", row["id"]).execute()
        for c in campaigns:
            sb.table("campaigns").upsert({"id": c["id"], "data": c}).execute()
    except Exception as e:
        print(f"[DB] save_data error: {e}")

# ---------- Geração de conteúdo (Grok) ----------

def _split_copy_cta(raw):
    """Separa COPY e CTA do retorno da IA no formato 'COPY: ...\nCTA: ...'"""
    msg_lines = []
    cta = ""
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped.upper().startswith("CTA:"):
            cta = stripped[4:].strip().strip('"\'')[:35]
        elif stripped.upper().startswith("COPY:"):
            msg_lines.append(stripped[5:].strip())
        else:
            msg_lines.append(stripped)
    return "\n".join(l for l in msg_lines if l), cta

def capitalize_lines(text):
    """Capitaliza a primeira letra de cada linha, como digitado no celular."""
    lines = text.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped:
            result.append(stripped[0].upper() + stripped[1:])
        else:
            result.append("")
    return "\n".join(result)

def generate_copy(persona, post_type, xai_key, used_texts=None):
    if not xai_key:
        return "", "Chave Grok/xAI não informada"

    historico = ""
    if used_texts:
        lista = "\n".join(f"- {t}" for t in used_texts[-10:])
        historico = (
            f"\n\nMensagens já geradas hoje (PROIBIDO repetir):\n{lista}"
            f"\n\nRegras anti-repetição OBRIGATÓRIAS:"
            f"\n- NÃO use o mesmo contexto/situação de nenhuma mensagem acima (ex: se já usou 'saindo do banho', não use banho de novo)"
            f"\n- NÃO comece com a mesma palavra ou estrutura de nenhuma mensagem acima"
            f"\n- NÃO use a mesma emoção ou gancho (ex: se já usou tédio, use outra coisa)"
            f"\n- O tema deve ser completamente diferente das mensagens acima"
        )

    temas_texto = [
        # Manhã / acordando
        "é de manhã cedo, ela acabou de acordar, ainda na cama, com preguiça e com vontade de outra coisa — fala isso de forma direta e curta",
        "acabou de acordar e já tá pensando em coisa errada, conta isso como se fosse mandar mensagem pra uma amiga",
        # Banho / se arrumando
        "saiu do banho agora, não tá com vontade de se vestir, conta isso de forma casual",
        "tá se arrumando pra sair mas tá em dúvida se vai ou fica em casa, e o motivo pra ficar é safado",
        # Tédio / em casa
        "tá entediada em casa, o dia tá vazio e aquela vontade não passa — conta isso sem forçar",
        "tá deitada no sofá, rolando o celular sem fazer nada, e aí bate aquela vontade do nada",
        # Bastidores / conteúdo
        "foi gravar hoje e aconteceu algo engraçado ou inesperado durante a gravação, conta como se fosse pra uma amiga",
        "acabou de ver um conteúdo que gravou e ficou surpresa com ela mesma, conta isso sem entregar o que é",
        # Confissão pessoal
        "conta uma vontade ou pensamento safado que teve hoje, de forma honesta e curta, sem exagero",
        "confessa uma coisa que fez ou pensou hoje que não deveria contar mas contou assim mesmo",
        # Situação do dia
        "algo simples aconteceu hoje — foi ao mercado, saiu, ficou em casa — mas pensou em algo safado no meio",
        "teve um dia comum mas uma cena do dia fez ela pensar em coisa errada, conta de forma natural",
        # Provocação leve
        "manda um pensamento curto e safado, como se fosse mensagem de zap pra alguém que ela gosta",
        "faz uma observação sobre como tá se sentindo agora, curta e direta, sem enrolação",
        # Madrugada
        "é tarde da noite ou madrugada, não consegue dormir e tá com a cabeça cheia de coisa errada",
        "tá acordada de madrugada e mandou uma mensagem que não devia — conta isso sem arrependimento",
        # Corpo / bem-estar
        "acabou de malhar ou fazer algo físico e o corpo tá pedindo uma coisa bem diferente de descanso",
        "tá com preguiça de tudo hoje menos de uma coisa, conta isso de forma direta",
        # Clima / ambiente
        "tá calor, tá em casa, tá com pouca roupa — conta isso como quem tá reclamando mas na verdade tá gostando",
        "o dia tá chuvoso e preguiçoso e ela tá num clima de ficar na cama fazendo outra coisa",
        # Humor / ironia
        "faz uma observação irônica e safada sobre algo do dia a dia, tom leve, sem forçar",
        "conta uma situação boba do dia que virou coisa safada na cabeça dela, com bom humor",
    ]
    temas_enquete = [
        "o que o seguidor quer ver dela hoje",
        "como o seguidor prefere ela — mais comportada ou mais soltinha",
        "o que o seguidor faria se estivesse com ela agora",
        "qual fantasia o seguidor mais quer ver ela realizar",
        "o que mais deixa o seguidor louco nela",
        "o que o seguidor acha que ela tá usando agora",
        "se o seguidor prefere vídeo curto ou longo dela",
        "o que o seguidor acha que ela vai postar hoje à noite",
    ]

    import random
    if post_type == "poll":
        tema = random.choice(temas_enquete)
        prompt = (
            f"Crie uma enquete pra canal Telegram adulto sobre: {tema}.\n"
            f"Primeira linha = a pergunta (curta, direta, como uma jovem brasileira falaria no zap).\n"
            f"Próximas 3-4 linhas = opções de resposta (curtas, safadas, naturais).\n"
            f"Escreve como uma pessoa real, não como marketing. Sem palavras como 'sensual', 'provocante', 'único', 'exótico'.\n"
            f"Persona de referência (tom, não copie literalmente): {persona[:300]}"
            f"{historico}"
        )
    else:
        tema = random.choice(temas_texto)
        prompt = (
            f"Escreva uma legenda de canal Telegram adulto sobre: {tema}.\n\n"
            f"EXEMPLOS do formato de saída OBRIGATÓRIO:\n"
            f"Exemplo 1:\n"
            f"COPY: Tô peladinha na cadeira... língua de fora pra te provocar 😜🔥\nEstou aqui te esperando pra me fazer companhia\nVem me ver toda peladinha sem censura no VIP\n"
            f"CTA: 🔥 Ver peladinha no VIP 🔥\n\n"
            f"Exemplo 2:\n"
            f"COPY: Tô entediada em casa, sem calcinha, rolando o celular kkkk\nAlguém pra me fazer companhia?\nTem coisa quente me esperando no VIP 🔥\n"
            f"CTA: 💦 Entrar no VIP agora 🔞\n\n"
            f"REGRAS DA COPY:\n"
            f"- Linhas curtas, ritmo de zap\n"
            f"- Primeira pessoa feminina, direta, sem vergonha\n"
            f"- NUNCA use 'mano', 'cara' referindo a ela\n"
            f"- NÃO use: 'sensual', 'provocante', 'sedutora', 'irresistível'\n"
            f"- NÃO mencione nome\n\n"
            f"REGRAS DO CTA:\n"
            f"- Máximo 25 caracteres\n"
            f"- Emoji no início e no final\n"
            f"- Contextualizado com o tema da copy\n\n"
            f"Persona de referência (tom): {persona[:200]}"
            f"{historico}"
        )

    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {xai_key}", "Content-Type": "application/json"},
            json={
                "model": "grok-4.3",
                "messages": [
                    {"role": "system", "content": "Você é uma garota brasileira de 20 e poucos anos com canal adulto pago no Telegram. Você escreve suas próprias legendas — na primeira pessoa, como mulher, de forma explícita e direta. Sem rodeios, sem eufemismos. Você descreve o que tá acontecendo com o corpo, o que tá sentindo, o que tá fazendo. Escreva APENAS o texto da legenda, sem aspas, sem introdução."},
                    {"role": "user",   "content": prompt},
                ],
                "max_tokens": 200,
                "temperature": 0.92,
            },
            timeout=20,
        )
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text[:300]}
        if not r.ok:
            err = data.get("error", data)
            if isinstance(err, dict):
                err = err.get("message") or err.get("error") or str(err)
            return "", "", f"xAI HTTP {r.status_code}: {err}"
        raw = data["choices"][0]["message"]["content"].strip()
        msg, cta = _split_copy_cta(raw)
        return capitalize_lines(msg), cta, ""
    except Exception as e:
        return "", "", str(e)


def generate_cta_label(msg, stype, xai_key):
    """CTA extraído da própria copy — sem chamada extra de IA."""
    # Chamada separada eliminada; o CTA agora vem embutido no retorno de generate_copy/generate_copy_vision
    # Esta função fica como fallback vazio para não quebrar chamadas legadas
    return ""


def generate_copy_vision(persona, image_url, xai_key, used_texts=None):
    if not xai_key:
        return "", "Chave Grok/xAI não informada"

    historico = ""
    if used_texts:
        lista = "\n".join(f"- {t}" for t in used_texts[-10:])
        historico = (
            f"\n\nMensagens já geradas hoje (PROIBIDO repetir):\n{lista}"
            f"\nA legenda deve ter contexto completamente diferente de todas as mensagens acima."
        )

    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {xai_key}", "Content-Type": "application/json"},
            json={
                "model": "grok-4.3",
                "messages": [
                    {"role": "system", "content": (
                        "Você é uma garota brasileira de 20 e poucos anos com canal adulto pago no Telegram. "
                        "Você escreve suas próprias legendas — na primeira pessoa, como mulher, de forma explícita e direta. "
                        "Você descreve o que tá acontecendo na cena, o que tá sentindo, o que tá fazendo com o corpo. "
                        "Sem rodeios. APENAS o texto, sem aspas, sem introdução."
                    )},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": (
                            "Olha essa foto e escreve a legenda pro canal adulto no Telegram.\n\n"
                            "EXEMPLOS DO ESTILO CERTO:\n"
                            "Foto no rio pelada: 'Tô aqui no rio bem peladinha\\nsentindo o sol no corpo e pensando em você\\nQuer ver tudo sem os foguinhos atrapalhando?\\nVEM VER TUDO NO VIP 👇'\n"
                            "Foto pelada à noite: 'Língua de fora pra te provocar 😜🔥\\nTô te esperando pra me fazer companhia\\nVem me ver sem censura no VIP'\n\n"
                            "COMO ESCREVER:\n"
                            "- Use NO MÁXIMO 1 detalhe visual da foto pra criar contexto (ex: 'no rio', 'à noite', 'só de calcinha')\n"
                            "- O resto da copy é sobre o que ela SENTE, QUER ou está PENSANDO — não descreva cada item da foto\n"
                            "- Linhas curtas, ritmo de zap, primeira pessoa feminina\n"
                            "- Termina com CTA contextual convidando pro VIP (ex: 'VEM VER TUDO NO VIP', 'VEM PRO VIP AGORA')\n"
                            "- Emojis naturais (🔥🥵😜💦😈)\n"
                            "- NÃO liste roupas, acessórios ou posições — isso vira inventário, não copy\n"
                            "- NÃO diga que tirou/mandou a foto\n"
                            "- NÃO use 'mano', 'cara'\n"
                            "- NÃO mencione nome\n\n"
                            "FORMATO DE SAÍDA OBRIGATÓRIO (duas partes separadas):\n"
                            "COPY: [legenda aqui]\n"
                            "CTA: [texto do botão — máx 25 chars, emoji no início e fim, contextualizado com a foto]\n\n"
                            f"Exemplos de CTA: '🔥 Ver sem censura no VIP 🔥', '💦 Vem ver tudo no VIP 🔞', '😈 Clica e vem ver 🔥'{historico}"
                        )},
                    ]},
                ],
                "max_tokens": 250,
                "temperature": 0.92,
            },
            timeout=40,
        )
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text[:300]}
        if not r.ok:
            err = data.get("error", data)
            if isinstance(err, dict):
                err = err.get("message") or err.get("error") or str(err)
            print(f"[Vision] Erro {r.status_code}: {err}")
            return "", "", f"xAI Vision HTTP {r.status_code}: {err}"
        raw = data["choices"][0]["message"]["content"].strip()
        msg, cta = _split_copy_cta(raw)
        msg = capitalize_lines(msg)
        print(f"[Vision] OK: {msg[:80]}")
        return msg, cta, ""
    except Exception as e:
        print(f"[Vision] Exception: {e}")
        return "", "", str(e)

# ---------- Telegram ----------

def send_text(token, chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=20)
    return r.json()

def send_photo(token, chat_id, url, caption="", reply_markup=None):
    payload = {"chat_id": chat_id, "photo": url, "caption": caption}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", json=payload, timeout=30)
    return r.json()

def send_video(token, chat_id, url, caption="", reply_markup=None):
    payload = {"chat_id": chat_id, "video": url, "caption": caption}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = requests.post(f"https://api.telegram.org/bot{token}/sendVideo", json=payload, timeout=60)
    return r.json()

def send_poll(token, chat_id, question, options):
    r = requests.post(f"https://api.telegram.org/bot{token}/sendPoll",
        json={"chat_id": chat_id, "question": question, "options": options}, timeout=20)
    return r.json()

# ---------- Execução ----------

def execute_schedule(campaign_id, hour, minute):
    campaigns = load_data()
    campaign  = next((c for c in campaigns if c["id"] == campaign_id), None)
    if not campaign or not campaign.get("active"):
        return
    today    = datetime.now(BRT).strftime("%Y-%m-%d")
    time_str = f"{hour:02d}:{minute:02d}"
    day_data = campaign.get("days", {}).get(today, {})
    slots    = day_data.get("slots", [])
    slot     = next((s for s in slots if s.get("time") == time_str), None)
    if not slot:
        print(f"[SKIP] {campaign_id[:8]} {today} {time_str} — sem slot configurado")
        return
    _run_slot(campaign, slot, today)

def _run_slot(campaign, slot, day):
    token     = campaign["token"]
    chat_id   = campaign["chat"]
    stype     = slot.get("type", "text")
    msg       = slot.get("msg", "").strip()
    media     = slot.get("media_path", "").strip()
    day_data  = campaign.get("days", {}).get(day, {})
    cta_label = slot.get("cta_label", "").strip() or day_data.get("cta_label", "").strip()
    cta_url   = campaign.get("cta_url", "").strip()
    print(f"[SEND] {stype} | {day} {slot.get('time')} | campaign={campaign['id'][:8]} | cta={'sim' if cta_label else 'não'}")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = _dispatch(token, chat_id, stype, media, msg, cta_label, cta_url)
            ok     = result.get("ok", False)
            detail = result.get("description", "Enviado" if ok else "Erro")
            log_entry(campaign["id"], slot["id"], ok, detail, attempt)
            return
        except requests.exceptions.Timeout:
            log_entry(campaign["id"], slot["id"], False, f"Timeout tentativa {attempt}", attempt)
        except Exception as e:
            log_entry(campaign["id"], slot["id"], False, str(e)[:80], attempt)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAYS[attempt - 1])

def _build_markup(cta_label, cta_url):
    if cta_label and cta_url:
        return {"inline_keyboard": [[{"text": cta_label, "url": cta_url}]]}
    return None

def _dispatch(token, chat_id, stype, media, msg, cta_label="", cta_url=""):
    # Polls não suportam inline keyboards no Telegram
    markup = None if stype == "poll" else _build_markup(cta_label, cta_url)
    if stype in ("image", "video"):
        if not media:
            return {"ok": False, "description": "Sem mídia configurada"}
        if stype == "image":
            return send_photo(token, chat_id, media, msg, markup)
        else:
            return send_video(token, chat_id, media, msg, markup)
    elif stype == "poll":
        lines    = [l.strip() for l in msg.split("\n") if l.strip()]
        question = lines[0] if lines else "O que você acha?"
        options  = lines[1:5] if len(lines) > 1 else ["Sim", "Não"]
        return send_poll(token, chat_id, question, options)
    else:
        return send_text(token, chat_id, msg or ".", markup)

def log_entry(campaign_id, slot_id, success, detail, attempt=1):
    with _log_lock:
        campaigns = load_data()
        for c in campaigns:
            if c["id"] == campaign_id:
                c.setdefault("logs", []).insert(0, {
                    "slot_id": slot_id,
                    "time":    datetime.now(BRT).isoformat(),
                    "success": success,
                    "detail":  detail,
                    "attempt": attempt,
                })
                c["logs"] = c["logs"][:100]
        save_data(campaigns)

def register_all_jobs():
    scheduler.remove_all_jobs()
    for c in load_data():
        if not c.get("active"):
            continue
        seen = set()
        for day_data in c.get("days", {}).values():
            for s in day_data.get("slots", []):
                t = s.get("time", "")
                if not t or ":" not in t or t in seen:
                    continue
                seen.add(t)
                h, m = map(int, t.split(":"))
                job_id = f"{c['id']}_{h:02d}{m:02d}"
                scheduler.add_job(
                    execute_schedule, "cron", hour=h, minute=m,
                    args=[c["id"], h, m],
                    id=job_id,
                    replace_existing=True,
                )

register_all_jobs()

# ---------- Rotas base ----------

@app.route("/api/ping")
def ping():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})

@app.route("/api/campaigns", methods=["GET"])
def get_campaigns():
    return jsonify(load_data())

@app.route("/api/campaigns", methods=["POST"])
def create_campaign():
    data = request.json
    data["id"]        = str(uuid.uuid4())
    data["createdAt"] = datetime.now().isoformat()
    data["logs"]      = []
    data["xai_key"]   = data.get("xai_key", "")
    data.setdefault("days", {})
    campaigns = load_data()
    campaigns.append(data)
    save_data(campaigns)
    register_all_jobs()
    return jsonify(data), 201

@app.route("/api/campaigns/<cid>", methods=["PUT"])
def update_campaign(cid):
    body      = request.json
    campaigns = load_data()
    for i, c in enumerate(campaigns):
        if c["id"] == cid:
            body["id"]        = cid
            body["createdAt"] = c.get("createdAt", datetime.now().isoformat())
            body["logs"]      = c.get("logs", [])
            body["xai_key"]   = body.get("xai_key", "")
            body.setdefault("days", c.get("days", {}))
            campaigns[i] = body
            save_data(campaigns)
            register_all_jobs()
            return jsonify(body)
    return jsonify({"error": "not found"}), 404

@app.route("/api/campaigns/<cid>", methods=["DELETE"])
def delete_campaign(cid):
    campaigns = [c for c in load_data() if c["id"] != cid]
    save_data(campaigns)
    register_all_jobs()
    return jsonify({"ok": True})

@app.route("/api/campaigns/<cid>/test", methods=["POST"])
def test_campaign(cid):
    campaigns = load_data()
    c = next((x for x in campaigns if x["id"] == cid), None)
    if not c:
        return jsonify({"ok": False, "error": "Campanha não encontrada"}), 404
    try:
        result = send_text(c["token"], c["chat"], "✅ Teste de conexão — bot funcionando!")
        ok     = result.get("ok", False)
        return jsonify({"ok": ok, "detail": result.get("description", ""), "raw": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/campaigns/<cid>/days", methods=["POST"])
def create_campaign_day(cid):
    day = (request.json or {}).get("day", "").strip()
    if not day:
        return jsonify({"error": "day required"}), 400
    campaigns = load_data()
    c = next((x for x in campaigns if x["id"] == cid), None)
    if not c:
        return jsonify({"error": "not found"}), 404
    c.setdefault("days", {})
    if day not in c["days"]:
        c["days"][day] = {"slots": []}
    save_data(campaigns)
    sb = get_sb()
    try:
        sb.storage.from_(BUCKET).upload(
            path=f"{day}/.keep",
            file=b"",
            file_options={"content-type": "text/plain", "upsert": "true"}
        )
    except Exception:
        pass
    register_all_jobs()
    return jsonify({"ok": True, "day": day})

@app.route("/api/campaigns/<cid>/days/<day>", methods=["GET"])
def get_campaign_day(cid, day):
    campaigns = load_data()
    c = next((x for x in campaigns if x["id"] == cid), None)
    if not c:
        return jsonify({"error": "not found"}), 404
    day_data = c.get("days", {}).get(day, {"slots": []})
    return jsonify(day_data)

@app.route("/api/campaigns/<cid>/days/<day>", methods=["PUT"])
def save_campaign_day(cid, day):
    body      = request.json or {}
    slots     = body.get("slots", [])
    campaigns = load_data()
    for c in campaigns:
        if c["id"] == cid:
            c.setdefault("days", {})
            c["days"][day] = {"slots": slots}
            save_data(campaigns)
            register_all_jobs()
            return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404

@app.route("/api/campaigns/<cid>/days/<day>", methods=["DELETE"])
def delete_campaign_day(cid, day):
    campaigns = load_data()
    for c in campaigns:
        if c["id"] == cid:
            c.get("days", {}).pop(day, None)
            save_data(campaigns)
            register_all_jobs()
            try:
                sb = get_sb()
                items = sb.storage.from_(BUCKET).list(path=day) or []
                files_to_delete = [f"{day}/{f['name']}" for f in items if f.get("name")]
                if files_to_delete:
                    sb.storage.from_(BUCKET).remove(files_to_delete)
            except Exception as e:
                print(f"[Storage] Erro ao apagar pasta {day}: {e}")
            return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404

@app.route("/api/campaigns/<cid>/days/<day>/send-now", methods=["POST"])
def send_now_slot(cid, day):
    slot_id = (request.json or {}).get("slot_id", "")
    campaigns = load_data()
    c = next((x for x in campaigns if x["id"] == cid), None)
    if not c:
        return jsonify({"error": "not found"}), 404
    slots = c.get("days", {}).get(day, {}).get("slots", [])
    slot  = next((s for s in slots if s["id"] == slot_id), None)
    if not slot:
        return jsonify({"error": "slot not found"}), 404
    import threading
    threading.Thread(target=_run_slot, args=(c, slot, day), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/campaigns/<cid>/logs", methods=["GET"])
def get_logs(cid):
    campaigns = load_data()
    c = next((x for x in campaigns if x["id"] == cid), None)
    return jsonify(c.get("logs", []) if c else [])

@app.route("/api/upload", methods=["POST"])
def upload_file():
    f   = request.files.get("file")
    day = request.form.get("day", "").strip()
    if not f:
        return jsonify({"error": "no file"}), 400
    ext          = os.path.splitext(f.filename)[1]
    unique_name  = str(uuid.uuid4()) + ext
    storage_path = f"{day}/{unique_name}" if day else unique_name
    contents     = f.read()
    sb = get_sb()
    sb.storage.from_(BUCKET).upload(
        path=storage_path,
        file=contents,
        file_options={"content-type": f.content_type or "application/octet-stream"}
    )
    public_url = sb.storage.from_(BUCKET).get_public_url(storage_path)
    return jsonify({"path": storage_path, "url": public_url, "name": f.filename, "storage_name": unique_name})

# ---------- Galeria ----------

@app.route("/api/media/days", methods=["POST"])
def create_storage_day():
    day = (request.json or {}).get("day", "").strip()
    if not day:
        return jsonify({"error": "day required"}), 400
    sb = get_sb()
    try:
        sb.storage.from_(BUCKET).upload(
            path=f"{day}/.keep",
            file=b"",
            file_options={"content-type": "text/plain", "upsert": "true"}
        )
    except Exception:
        pass
    return jsonify({"ok": True, "day": day})

@app.route("/api/media/days/<path:day>", methods=["DELETE"])
def delete_storage_day(day):
    sb = get_sb()
    try:
        items = sb.storage.from_(BUCKET).list(path=day) or []
        paths = [f"{day}/{f['name']}" for f in items if f.get("name")]
        if paths:
            sb.storage.from_(BUCKET).remove(paths)
    except Exception as e:
        print(f"[Gallery] delete day error: {e}")
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "day": day})

# Emojis pré-baixados em cache (evita download repetido)
_EMOJI_CACHE = {}

def _get_emoji_img(size=120):
    """Retorna imagem PIL do emoji 🔥 (Twemoji PNG)."""
    from PIL import Image
    import io
    key = size
    if key not in _EMOJI_CACHE:
        url = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14/assets/72x72/1f525.png"
        r = requests.get(url, timeout=10)
        img = Image.open(io.BytesIO(r.content)).convert("RGBA").resize((size, size), Image.LANCZOS)
        _EMOJI_CACHE[key] = img
    return _EMOJI_CACHE[key]

def _detect_explicit_areas(image_url, xai_key):
    """Usa Vision API pra detectar partes explícitas e retornar coordenadas (0.0–1.0)."""
    import json as _json
    prompt = (
        "Look at this image carefully. Identify any explicit body parts that need censoring "
        "(bare breasts, nipples, genitals, buttocks if fully exposed).\n\n"
        "Return ONLY a valid JSON array. Each item: {\"x\": 0.5, \"y\": 0.3, \"r\": 0.12} "
        "where x = horizontal center (0=left, 1=right), y = vertical center (0=top, 1=bottom), "
        "r = radius as fraction of image width.\n\n"
        "If nothing explicit: return []\n"
        "Return ONLY the JSON array, no explanation."
    )
    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {xai_key}", "Content-Type": "application/json"},
            json={
                "model": "grok-4.3",
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": prompt},
                ]}],
                "max_tokens": 300,
                "temperature": 0.1,
            },
            timeout=40,
        )
        data = r.json()
        if not r.ok:
            return [], str(data)
        raw = data["choices"][0]["message"]["content"].strip()
        # Extrai JSON mesmo se vier com texto ao redor
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1:
            return [], "no json"
        areas = _json.loads(raw[start:end])
        return areas, ""
    except Exception as e:
        return [], str(e)

def _apply_emojis(image_bytes, areas, img_emoji):
    """Sobrepõe emojis nas áreas indicadas e retorna bytes JPEG."""
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size
    for area in areas:
        x_frac = float(area.get("x", 0.5))
        y_frac = float(area.get("y", 0.5))
        r_frac = float(area.get("r", 0.1))
        size   = max(60, int(r_frac * w * 2.2))  # diâmetro com margem
        emoji  = img_emoji.resize((size, size), Image.LANCZOS)
        cx = int(x_frac * w) - size // 2
        cy = int(y_frac * h) - size // 2
        img.paste(emoji, (cx, cy), emoji)
    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=90)
    return buf.getvalue()

@app.route("/api/media/days/<path:day>/censor", methods=["POST"])
def censor_day_photos(day):
    xai_key = (request.json or {}).get("xai_key", "")
    if not xai_key:
        return jsonify({"error": "Chave xAI não informada"}), 400
    sb = get_sb()
    try:
        day_files = sb.storage.from_(BUCKET).list(path=day) or []
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    photos = [f for f in day_files if f.get("name") and not f["name"].startswith(".")]
    results = []
    try:
        emoji_img = _get_emoji_img(120)
    except Exception as e:
        return jsonify({"error": f"Erro ao carregar emoji: {e}"}), 500

    for f in photos:
        fname     = f["name"]
        full_path = f"{day}/{fname}"
        public_url = sb.storage.from_(BUCKET).get_public_url(full_path)
        try:
            # 1. Detecta áreas explícitas
            areas, err = _detect_explicit_areas(public_url, xai_key)
            if err:
                results.append({"file": fname, "ok": False, "error": err})
                continue
            if not areas:
                results.append({"file": fname, "ok": True, "censored": False, "msg": "nenhuma área detectada"})
                continue
            # 2. Baixa imagem original
            img_resp = requests.get(public_url, timeout=20)
            img_resp.raise_for_status()
            # 3. Aplica emojis
            censored_bytes = _apply_emojis(img_resp.content, areas, emoji_img)
            # 4. Sobe imagem censurada substituindo o arquivo original
            sb.storage.from_(BUCKET).remove([full_path])
            sb.storage.from_(BUCKET).upload(
                path=full_path,
                file=censored_bytes,
                file_options={"content-type": "image/jpeg", "upsert": "true"},
            )
            results.append({"file": fname, "ok": True, "censored": True, "areas": len(areas)})
        except Exception as e:
            results.append({"file": fname, "ok": False, "error": str(e)})

    total    = len(results)
    censored = sum(1 for r in results if r.get("censored"))
    return jsonify({"ok": True, "total": total, "censored": censored, "results": results})

@app.route("/api/media", methods=["GET"])
def list_media():
    campaign_id = request.args.get("campaign_id", "")
    real_sb     = get_sb()
    posted_by_day  = {}
    campaign_days_filter = None
    if campaign_id:
        campaigns = load_data()
        c = next((x for x in campaigns if x["id"] == campaign_id), None)
        if c:
            campaign_days_filter = set(c.get("days", {}).keys())
            for day, day_data in c.get("days", {}).items():
                used = set()
                for s in day_data.get("slots", []):
                    mp = s.get("media_path", "")
                    if not mp:
                        continue
                    # media_path pode ser URL completa ou só o path; normalizamos pra path relativo
                    if "/object/public/" in mp:
                        mp = mp.split(f"/{BUCKET}/", 1)[-1]
                    used.add(mp)
                posted_by_day[day] = used
    total_bytes = 0
    days_result = []
    try:
        root_items = real_sb.storage.from_(BUCKET).list() or []
    except Exception as e:
        print(f"[Gallery] list root error: {e}")
        root_items = []
    day_folders = [item.get("name") for item in root_items if item.get("metadata") is None and item.get("name")]
    for day in sorted(day_folders, reverse=True):
        # Nunca filtra pastas — mostra tudo do storage
        # "posted_set" é calculado com base na campanha selecionada (se houver)
        posted_set = posted_by_day.get(day, set())
        try:
            day_items = real_sb.storage.from_(BUCKET).list(path=day) or []
        except Exception:
            day_items = []
        files = []
        for f in sorted(day_items, key=lambda x: x.get("name", "")):
            fname = f.get("name", "")
            if not fname or fname.startswith("."):
                continue
            meta = f.get("metadata") or {}
            size = meta.get("size") or 0
            total_bytes += size
            full_path = f"{day}/{fname}"
            files.append({
                "name":   fname,
                "path":   full_path,
                "size":   size,
                "url":    real_sb.storage.from_(BUCKET).get_public_url(full_path),
                "posted": full_path in posted_set,
            })
        days_result.append({
            "day":          day,
            "files":        files,
            "total":        len(files),
            "posted_count": len([f for f in files if f["posted"]]),
        })
    return jsonify({
        "days":        days_result,
        "total_bytes": total_bytes,
        "limit_bytes": 1_073_741_824,
    })

@app.route("/api/media/<path:name>", methods=["DELETE"])
def delete_media(name):
    get_sb().storage.from_(BUCKET).remove([name])
    return jsonify({"ok": True})

# ---------- IA ----------

@app.route("/api/validate-token", methods=["POST"])
def validate_token():
    token = request.json.get("token", "")
    try:
        r    = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = r.json()
        if data.get("ok"):
            bot = data["result"]
            return jsonify({"ok": True, "username": bot.get("username"), "name": bot.get("first_name")})
        return jsonify({"ok": False, "error": data.get("description", "Token inválido")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/campaigns/<cid>/days/<day>/slot-configs", methods=["GET"])
def get_slot_configs(cid, day):
    DAY_TYPES = [
        "image","text","image","poll",
        "image","image","text","image",
        "image","poll","image","text",
        "image","image","text","image",
        "image","poll","image","image",
        "text","image","image","text",
    ]
    # from_hour: só atribui fotos a slots a partir dessa hora
    from_hour = int(request.args.get("from_hour", 0))
    sb = get_sb()
    try:
        day_files = sb.storage.from_(BUCKET).list(path=day) or []
    except Exception:
        day_files = []
    photos = sorted(
        [f for f in day_files if f.get("name") and not f["name"].startswith(".")],
        key=lambda f: f.get("name", "")
    )
    photo_idx = 0
    configs = []
    for hour, stype in enumerate(DAY_TYPES):
        media_path = ""
        media_name = ""
        # Só atribui foto se o slot for >= from_hour
        if stype in ("image", "video") and hour >= from_hour and photo_idx < len(photos):
            fname      = photos[photo_idx]["name"]
            full_path  = f"{day}/{fname}"
            media_path = sb.storage.from_(BUCKET).get_public_url(full_path)
            media_name = fname
            photo_idx += 1
        configs.append({
            "id":         str(uuid.uuid4()),
            "hour":       hour,
            "type":       stype,
            "media_path": media_path,
            "media_name": media_name,
        })
    return jsonify({"ok": True, "configs": configs})

@app.route("/api/generate-slot-copy", methods=["POST"])
def api_generate_slot_copy():
    body       = request.json or {}
    persona    = body.get("persona", "")
    xai_key    = body.get("xai_key", "")
    stype      = body.get("type", "text")
    media_path = body.get("media_path", "")
    used_texts = body.get("used_texts", []) or []
    if not xai_key:
        return jsonify({"ok": False, "error": "Chave Grok não informada"})
    vision_err = ""
    msg = ""
    cta_label = ""
    if stype in ("image", "video") and media_path:
        msg, cta_label, vision_err = generate_copy_vision(persona, media_path, xai_key, used_texts=used_texts)
        if not msg:
            print(f"[Vision] Falhou ({vision_err}), usando fallback texto")
            msg, cta_label, _ = generate_copy(persona, stype, xai_key, used_texts=used_texts)
    else:
        msg, cta_label, vision_err = generate_copy(persona, stype, xai_key, used_texts=used_texts)
    return jsonify({"ok": bool(msg), "msg": msg, "cta_label": cta_label, "vision_err": vision_err})

@app.route("/api/generate-copy", methods=["POST"])
def api_generate_copy():
    body      = request.json
    persona   = body.get("persona", "")
    post_type = body.get("type", "image")
    xai_key   = body.get("xai_key", "")
    if not xai_key:
        return jsonify({"ok": False, "error": "Chave Grok/xAI não informada"}), 400
    copy, error = generate_copy(persona, post_type, xai_key)
    return jsonify({"ok": bool(copy), "copy": copy, "error": error})

@app.route("/api/generate-day", methods=["POST"])
def api_generate_day():
    body    = request.json or {}
    persona = body.get("persona", "")
    xai_key = body.get("xai_key", "")
    day     = body.get("day", datetime.now().strftime("%Y-%m-%d"))
    DAY_TYPES = [
        "image","text","image","poll",
        "image","image","text","image",
        "image","poll","image","text",
        "image","image","text","image",
        "image","poll","image","image",
        "text","image","image","text",
    ]
    sb = get_sb()
    try:
        day_files = sb.storage.from_(BUCKET).list(path=day) or []
    except Exception:
        day_files = []
    photos = sorted(
        [f for f in day_files if f.get("name") and not f["name"].startswith(".")],
        key=lambda f: f.get("name", "")
    )
    photo_idx = 0
    slot_configs = []
    for hour, stype in enumerate(DAY_TYPES):
        media_path = ""
        media_name = ""
        if stype in ("image", "video") and photo_idx < len(photos):
            fname      = photos[photo_idx]["name"]
            full_path  = f"{day}/{fname}"
            media_path = sb.storage.from_(BUCKET).get_public_url(full_path)
            media_name = fname
            photo_idx += 1
        slot_configs.append({
            "id":         str(uuid.uuid4()),
            "hour":       hour,
            "type":       stype,
            "media_path": media_path,
            "media_name": media_name,
        })

    def generate_for_slot(cfg):
        if not (xai_key and persona):
            return cfg, "", ""
        stype = cfg["type"]
        if stype in ("image", "video") and cfg["media_path"]:
            msg, cta, _ = generate_copy_vision(persona, cfg["media_path"], xai_key)
        else:
            msg, cta, _ = generate_copy(persona, stype, xai_key)
        return cfg, msg, cta

    results = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(generate_for_slot, cfg): cfg for cfg in slot_configs}
        for future in as_completed(futures):
            cfg, msg, cta = future.result()
            results[cfg["id"]] = {"msg": msg, "cta_label": cta}

    slots = [
        {
            "id":         cfg["id"],
            "time":       f"{cfg['hour']:02d}:00",
            "type":       cfg["type"],
            "msg":        results.get(cfg["id"], {}).get("msg", ""),
            "cta_label":  results.get(cfg["id"], {}).get("cta_label", ""),
            "media_path": cfg["media_path"],
            "media_name": cfg["media_name"],
        }
        for cfg in slot_configs
    ]
    return jsonify({"ok": True, "slots": slots, "day": day})

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

if __name__ == "__main__":
    print("\n  Gestor Telegram rodando em http://localhost:5000\n")
    app.run(debug=False, port=5000)
