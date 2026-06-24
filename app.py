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

DEFAULT_DAY_TYPES = [
    "image","reaction","image","poll",
    "image","image","reaction","image",
    "image","poll","image","reaction",
    "image","image","reaction","image",
    "image","poll","image","image",
    "reaction","image","image","reaction",
]

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

# ---------- Helpers de mídia por dia ----------

def pick_next_media(campaign, date):
    """Retorna a próxima foto não postada da pasta do dia."""
    sb = get_sb()
    try:
        day_files = sb.storage.from_(BUCKET).list(path=date) or []
    except Exception as e:
        print(f"[MEDIA] Erro ao listar pasta {date}: {e}")
        return None

    files = sorted(
        [f for f in day_files if f.get("name") and not f["name"].startswith(".")],
        key=lambda f: f.get("name", "")
    )
    if not files:
        return None

    posted = set(campaign.get("media_posted", {}).get(date, []))
    for f in files:
        if f["name"] not in posted:
            full_path = f"{date}/{f['name']}"
            url = sb.storage.from_(BUCKET).get_public_url(full_path)
            return {"name": f["name"], "path": full_path, "url": url}

    return None  # Todas as fotos já foram postadas

def mark_media_posted(campaign_id, date, filename):
    """Marca uma foto como postada no histórico da campanha."""
    campaigns = load_data()
    for c in campaigns:
        if c["id"] == campaign_id:
            mp = c.setdefault("media_posted", {})
            mp.setdefault(date, [])
            if filename not in mp[date]:
                mp[date].append(filename)
    save_data(campaigns)

# ---------- Geração de conteúdo (Grok) ----------

def generate_copy(persona, post_type, xai_key):
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
                "model": "grok-3",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                "max_tokens": 200,
                "temperature": 0.85,
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
            return "", f"xAI HTTP {r.status_code}: {err}"
        return data["choices"][0]["message"]["content"].strip(), ""
    except Exception as e:
        return "", str(e)

# ---------- Telegram ----------

def send_text(token, chat_id, text):
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text}, timeout=20)
    return r.json()

def send_photo(token, chat_id, path_or_url, caption=""):
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

# ---------- Envio automático ----------

def execute_schedule(campaign_id, schedule_id):
    campaigns = load_data()
    campaign  = next((c for c in campaigns if c["id"] == campaign_id), None)
    if not campaign or not campaign.get("active"):
        return
    schedule = next((s for s in campaign.get("schedules", []) if s["id"] == schedule_id), None)
    if not schedule:
        return

    token   = campaign["token"]
    chat_id = campaign["chat"]
    stype   = schedule.get("type", "text")
    xai_key = campaign.get("xai_key", "")
    persona = campaign.get("persona", "")
    today   = datetime.now().strftime("%Y-%m-%d")

    print(f"[SEND] {stype} | campaign={campaign_id[:8]} | {today}")

    # ── Imagem / Vídeo ──────────────────────────────────────────
    if stype in ("image", "video"):
        media = pick_next_media(campaign, today)
        if not media:
            detail = f"Sem fotos disponíveis na pasta {today}"
            print(f"[SEND] {detail}")
            log_entry(campaign_id, schedule_id, False, detail)
            return

        # Gera legenda com Grok (se configurado)
        caption = ""
        if xai_key and persona:
            caption, err = generate_copy(persona, stype, xai_key)
            if err:
                print(f"[GROK] {err}")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if stype == "image":
                    result = send_photo(token, chat_id, media["url"], caption)
                else:
                    result = send_video(token, chat_id, media["url"], caption)
                ok     = result.get("ok", False)
                detail = result.get("description", "Enviado" if ok else "Erro")
                if ok:
                    mark_media_posted(campaign_id, today, media["name"])
                log_entry(campaign_id, schedule_id, ok, detail, attempt)
                return
            except requests.exceptions.Timeout:
                log_entry(campaign_id, schedule_id, False, f"Timeout tentativa {attempt}", attempt)
            except Exception as e:
                log_entry(campaign_id, schedule_id, False, str(e)[:80], attempt)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAYS[attempt - 1])
        return

    # ── Reação / Texto ──────────────────────────────────────────
    if stype in ("reaction", "text"):
        msg = schedule.get("msg", "").strip()
        if not msg:
            if xai_key and persona:
                msg, err = generate_copy(persona, stype, xai_key)
                if err:
                    print(f"[GROK] {err}")
            if not msg:
                msg = "Post agendado"

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = send_text(token, chat_id, msg)
                ok     = result.get("ok", False)
                detail = result.get("description", "Enviado" if ok else "Erro")
                log_entry(campaign_id, schedule_id, ok, detail, attempt)
                return
            except requests.exceptions.Timeout:
                log_entry(campaign_id, schedule_id, False, f"Timeout tentativa {attempt}", attempt)
            except Exception as e:
                log_entry(campaign_id, schedule_id, False, str(e)[:80], attempt)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAYS[attempt - 1])
        return

    # ── Enquete ─────────────────────────────────────────────────
    if stype == "poll":
        msg = schedule.get("msg", "").strip()
        if not msg:
            if xai_key and persona:
                msg, err = generate_copy(persona, "poll", xai_key)
                if err:
                    print(f"[GROK] {err}")

        lines    = [l.strip() for l in msg.split("\n") if l.strip()]
        question = lines[0] if lines else "O que você acha?"
        options  = lines[1:5] if len(lines) > 1 else ["Sim", "Não"]

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = send_poll(token, chat_id, question, options)
                ok     = result.get("ok", False)
                detail = result.get("description", "Enviado" if ok else "Erro")
                log_entry(campaign_id, schedule_id, ok, detail, attempt)
                return
            except requests.exceptions.Timeout:
                log_entry(campaign_id, schedule_id, False, f"Timeout tentativa {attempt}", attempt)
            except Exception as e:
                log_entry(campaign_id, schedule_id, False, str(e)[:80], attempt)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAYS[attempt - 1])

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

def build_default_schedules():
    return [{
        "id":         str(uuid.uuid4()),
        "time":       f"{hour:02d}:00",
        "type":       stype,
        "msg":        "",
        "media_path": "",
        "media_name": "",
    } for hour, stype in enumerate(DEFAULT_DAY_TYPES)]

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
    return jsonify({"ok": True, "time": datetime.now().isoformat()})

@app.route("/api/campaigns", methods=["GET"])
def get_campaigns():
    return jsonify(load_data())

@app.route("/api/campaigns", methods=["POST"])
def create_campaign():
    data = request.json
    data["id"]           = str(uuid.uuid4())
    data["createdAt"]    = datetime.now().isoformat()
    data["logs"]         = []
    data["media_posted"] = {}
    data["xai_key"]      = data.get("xai_key", "")
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
            body["id"]           = cid
            body["createdAt"]    = c.get("createdAt", datetime.now().isoformat())
            body["logs"]         = c.get("logs", [])
            body["media_posted"] = c.get("media_posted", {})
            body["xai_key"]      = body.get("xai_key", "")
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
        result = send_text(c["token"], c["chat"], "✅ Teste de conexão — bot funcionando!")
        ok     = result.get("ok", False)
        return jsonify({"ok": ok, "detail": result.get("description", ""), "raw": result})
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

# ---------- Upload ----------

@app.route("/api/upload", methods=["POST"])
def upload_file():
    f   = request.files.get("file")
    day = request.form.get("day", "").strip()   # YYYY-MM-DD opcional
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
    return jsonify({"path": public_url, "name": f.filename, "storage_name": unique_name})

# ---------- Galeria por dia ----------

@app.route("/api/media/days", methods=["POST"])
def create_day():
    """Cria uma pasta de dia no Storage com um arquivo .keep."""
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
        pass  # Pasta já existe
    return jsonify({"ok": True, "day": day})

@app.route("/api/media", methods=["GET"])
def list_media():
    campaign_id = request.args.get("campaign_id", "")
    real_sb     = get_sb()

    # Posted map para esta campanha
    posted_by_day = {}
    if campaign_id:
        campaigns = load_data()
        c = next((x for x in campaigns if x["id"] == campaign_id), None)
        if c:
            posted_by_day = c.get("media_posted", {})

    total_bytes = 0
    days_result = []
    root_files  = []

    root_items = real_sb.storage.from_(BUCKET).list() or []
    day_folders = []

    for item in root_items:
        name = item.get("name", "")
        if not name:
            continue
        meta = item.get("metadata")
        if meta is None:
            # É uma "pasta" (dia)
            day_folders.append(name)
        elif not name.startswith("."):
            size = meta.get("size") or 0
            total_bytes += size
            root_files.append({
                "name": name, "size": size,
                "url": real_sb.storage.from_(BUCKET).get_public_url(name),
                "posted": False,
            })

    # Lista arquivos de cada pasta de dia
    for day in sorted(day_folders, reverse=True):
        posted_set = set(posted_by_day.get(day, []))
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
            files.append({
                "name":    fname,
                "path":    f"{day}/{fname}",
                "size":    size,
                "url":     real_sb.storage.from_(BUCKET).get_public_url(f"{day}/{fname}"),
                "posted":  fname in posted_set,
            })

        days_result.append({
            "day":           day,
            "files":         files,
            "total":         len(files),
            "posted_count":  len([f for f in files if f["posted"]]),
        })

    return jsonify({
        "days":        days_result,
        "root_files":  root_files,
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
            "id": str(uuid.uuid4()), "time": f"{hour:02d}:00",
            "type": stype, "msg": msg, "media_path": "", "media_name": "",
        })
    return jsonify({"ok": True, "schedules": schedules})

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

if __name__ == "__main__":
    print("\n  Gestor Telegram rodando em http://localhost:5000\n")
    app.run(debug=False, port=5000)
