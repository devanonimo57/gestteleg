import os, uuid, time, requests
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
from supabase import create_client

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BUCKET       = "media"

def get_sb():
    return create_client(SUPABASE_URL, SUPABASE_KEY)

scheduler = BackgroundScheduler()
scheduler.start()

MAX_RETRIES  = 5
RETRY_DELAYS = [5, 10, 30, 60, 120]

# Distribuição padrão de 24 horários: 16 imagens, 5 reações, 3 enquetes
DEFAULT_DAY_TYPES = [
    "image","reaction","image","poll",
    "image","image","reaction","image",
    "image","poll","image","reaction",
    "image","image","reaction","image",
    "image","poll","image","image",
    "reaction","image","image","reaction",
]

# ---------- Banco de dados (Supabase) ----------

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

        # Remove campanhas deletadas
        existing = sb.table("campaigns").select("id").execute()
        for row in existing.data:
            if row["id"] not in new_ids:
                sb.table("campaigns").delete().eq("id", row["id"]).execute()

        # Upsert todas as campanhas
        for c in campaigns:
            sb.table("campaigns").upsert({"id": c["id"], "data": c}).execute()
    except Exception as e:
        print(f"[DB] save_data error: {e}")

# ---------- Helpers ----------

def build_default_schedules():
    schedules = []
    for hour, stype in enumerate(DEFAULT_DAY_TYPES):
        schedules.append({
            "id": str(uuid.uuid4()),
            "time": f"{hour:02d}:00",
            "type": stype,
            "msg": "",
            "media_path": "",
            "media_name": "",
        })
    return schedules

def generate_copy(persona, post_type, xai_key):
    """Gera copy via Grok usando a persona da campanha."""
    if not xai_key:
        return "", "Chave Grok/xAI nao informada"

    type_hints = {
        "image":    "uma legenda curta e envolvente para uma imagem",
        "reaction": "uma mensagem curta que gere reação e engajamento",
        "poll":     "uma enquete com pergunta na primeira linha e 3 opções nas linhas seguintes",
        "text":     "uma mensagem de texto curta e envolvente",
        "video":    "uma legenda curta e envolvente para um vídeo",
    }
    hint = type_hints.get(post_type, "uma mensagem curta")

    system = f"""Você é um redator de conteúdo para Telegram.
Persona do canal: {persona}
Escreva APENAS o texto do post, sem explicações, sem aspas, sem introdução.
Para enquetes: primeira linha = pergunta, próximas linhas = opções (máx 4)."""

    prompt = f"Crie {hint} para o canal. Seja criativo, direto e no estilo da persona."

    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {xai_key}", "Content-Type": "application/json"},
            json={
                "model": "grok-4.3",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                "max_tokens": 200,
                "temperature": 0.85,
            },
            timeout=15,
        )
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text[:300]}
        if not r.ok:
            print(f"[GROK ERROR] {data}")
            err = data.get("error", data)
            if isinstance(err, dict):
                err = err.get("message") or err.get("error") or str(err)
            return "", f"xAI HTTP {r.status_code}: {err}"
        return data["choices"][0]["message"]["content"].strip(), ""
    except Exception as e:
        print(f"[GROK ERROR] {e}")
        return "", str(e)

# ---------- Telegram ----------

def send_text(token, chat_id, text):
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text}, timeout=20)
    return r.json()

def send_photo(token, chat_id, path_or_url, caption=""):
    # Supabase Storage devolve URL pública — usa direto no Telegram
    if path_or_url.startswith("http"):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
            json={"chat_id": chat_id, "photo": path_or_url, "caption": caption}, timeout=30)
    else:
        with open(path_or_url, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f}, timeout=30)
    return r.json()

def send_video(token, chat_id, path_or_url, caption=""):
    if path_or_url.startswith("http"):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendVideo",
            json={"chat_id": chat_id, "video": path_or_url, "caption": caption}, timeout=60)
    else:
        with open(path_or_url, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendVideo",
                data={"chat_id": chat_id, "caption": caption},
                files={"video": f}, timeout=60)
    return r.json()

def send_poll(token, chat_id, question, options):
    r = requests.post(f"https://api.telegram.org/bot{token}/sendPoll",
        json={"chat_id": chat_id, "question": question, "options": options}, timeout=20)
    return r.json()

def do_send(campaign, schedule):
    token   = campaign["token"]
    chat_id = campaign["chat"]
    stype   = schedule.get("type", "text")
    msg     = schedule.get("msg", "") or ""
    media   = schedule.get("media_path", "") or ""

    if stype in ("image", "video") and media:
        result = send_photo(token, chat_id, media, msg) if stype == "image" else send_video(token, chat_id, media, msg)
    elif stype == "poll":
        lines    = [l.strip() for l in msg.split("\n") if l.strip()]
        question = lines[0] if lines else "Enquete"
        options  = lines[1:5] if len(lines) > 1 else ["Sim", "Nao"]
        result   = send_poll(token, chat_id, question, options)
    else:
        result = send_text(token, chat_id, msg or "Post agendado")

    ok     = result.get("ok", False)
    detail = result.get("description", "Enviado com sucesso" if ok else "Erro desconhecido")
    return ok, detail

def log_entry(campaign_id, schedule_id, success, detail, attempt=1):
    campaigns = load_data()
    for c in campaigns:
        if c["id"] == campaign_id:
            c.setdefault("logs", []).insert(0, {
                "schedule_id": schedule_id,
                "time":        datetime.now().isoformat(),
                "success":     success,
                "detail":      detail,
                "attempt":     attempt,
            })
            c["logs"] = c["logs"][:100]
    save_data(campaigns)

def execute_schedule(campaign_id, schedule_id):
    campaigns = load_data()
    campaign  = next((c for c in campaigns if c["id"] == campaign_id), None)
    if not campaign or not campaign.get("active"):
        return
    schedule  = next((s for s in campaign.get("schedules", []) if s["id"] == schedule_id), None)
    if not schedule:
        return

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"[SEND] attempt={attempt}/{MAX_RETRIES} campaign={campaign_id[:8]} schedule={schedule_id[:8]}")
            ok, detail = do_send(campaign, schedule)
            log_entry(campaign_id, schedule_id, ok, detail, attempt)
            if ok:
                print(f"[SEND] enviado na tentativa {attempt}")
                return
            print(f"[SEND] erro do Telegram: {detail}")
            return
        except requests.exceptions.Timeout:
            detail = f"Timeout na tentativa {attempt}/{MAX_RETRIES}"
            log_entry(campaign_id, schedule_id, False, detail, attempt)
        except requests.exceptions.ConnectionError:
            detail = f"Erro de conexao na tentativa {attempt}/{MAX_RETRIES}"
            log_entry(campaign_id, schedule_id, False, detail, attempt)
        except Exception as e:
            detail = f"Erro inesperado: {str(e)[:80]}"
            log_entry(campaign_id, schedule_id, False, detail, attempt)

        if attempt < MAX_RETRIES:
            wait = RETRY_DELAYS[attempt - 1]
            print(f"[SEND] aguardando {wait}s antes da tentativa {attempt+1}...")
            time.sleep(wait)

    print(f"[SEND] falhou apos {MAX_RETRIES} tentativas")

def register_all_jobs():
    scheduler.remove_all_jobs()
    for c in load_data():
        if not c.get("active"):
            continue
        for s in c.get("schedules", []):
            t = s.get("time", "")
            if not t or ":" not in t:
                continue
            h, m = map(int, t.split(":"))
            scheduler.add_job(
                execute_schedule, "cron", hour=h, minute=m,
                args=[c["id"], s["id"]],
                id=f"{c['id']}_{s['id']}",
                replace_existing=True,
            )

register_all_jobs()

# ---------- Rotas ----------

@app.route("/api/ping")
def ping():
    """Usado pelo UptimeRobot para manter o serviço acordado."""
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
    if not data.get("schedules"):
        data["schedules"] = build_default_schedules()
    for s in data.get("schedules", []):
        s.setdefault("id", str(uuid.uuid4()))
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
            if not body.get("schedules"):
                body["schedules"] = build_default_schedules()
            for s in body.get("schedules", []):
                s.setdefault("id", str(uuid.uuid4()))
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
        return jsonify({"ok": False, "error": "Campanha nao encontrada"}), 404
    try:
        result = send_text(c["token"], c["chat"], "Teste de conexao - bot funcionando!")
        ok     = result.get("ok", False)
        detail = result.get("description", "")
        return jsonify({"ok": ok, "detail": detail, "raw": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/campaigns/<cid>/send-now/<sid>", methods=["POST"])
def send_now(cid, sid):
    import threading
    threading.Thread(target=execute_schedule, args=(cid, sid), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/campaigns/<cid>/logs", methods=["GET"])
def get_logs(cid):
    campaigns = load_data()
    c = next((x for x in campaigns if x["id"] == cid), None)
    return jsonify(c.get("logs", []) if c else [])

@app.route("/api/upload", methods=["POST"])
def upload_file():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    ext      = os.path.splitext(f.filename)[1]
    name     = str(uuid.uuid4()) + ext
    contents = f.read()
    sb = get_sb()
    sb.storage.from_(BUCKET).upload(
        path=name,
        file=contents,
        file_options={"content-type": f.content_type or "application/octet-stream"}
    )
    public_url = sb.storage.from_(BUCKET).get_public_url(name)
    return jsonify({"path": public_url, "name": f.filename})

@app.route("/api/validate-token", methods=["POST"])
def validate_token():
    token = request.json.get("token", "")
    try:
        r    = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = r.json()
        if data.get("ok"):
            bot = data["result"]
            return jsonify({"ok": True, "username": bot.get("username"), "name": bot.get("first_name")})
        return jsonify({"ok": False, "error": data.get("description", "Token invalido")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/generate-copy", methods=["POST"])
def api_generate_copy():
    body      = request.json
    persona   = body.get("persona", "")
    post_type = body.get("type", "image")
    xai_key   = body.get("xai_key", "")
    if not xai_key:
        return jsonify({"ok": False, "error": "Chave Grok/xAI nao informada"}), 400
    copy, error = generate_copy(persona, post_type, xai_key)
    return jsonify({"ok": bool(copy), "copy": copy, "error": error})

@app.route("/api/generate-day", methods=["POST"])
def api_generate_day():
    body    = request.json or {}
    persona = body.get("persona", "")
    xai_key = body.get("xai_key", "")
    schedules = []
    for hour, stype in enumerate(DEFAULT_DAY_TYPES):
        msg = ""
        if xai_key and persona:
            msg, _ = generate_copy(persona, stype, xai_key)
        schedules.append({
            "id":         str(uuid.uuid4()),
            "time":       f"{hour:02d}:00",
            "type":       stype,
            "msg":        msg,
            "media_path": "",
            "media_name": "",
        })
    return jsonify({"ok": True, "schedules": schedules})

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

if __name__ == "__main__":
    print("\n  Gestor Telegram rodando em http://localhost:5000\n")
    app.run(debug=False, port=5000)
