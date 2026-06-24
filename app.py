import os, uuid, time, requests, base64
from concurrent.futures import ThreadPoolExecutor, as_completed
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

def generate_copy(persona, post_type, xai_key, used_texts=None):
    if not xai_key:
        return "", "Chave Grok/xAI não informada"

    historico = ""
    if used_texts:
        lista = "\n".join(f"- {t}" for t in used_texts[-10:])
        historico = f"\n\nTextos já usados (NÃO repita ideias, ganchos ou frases similares):\n{lista}"

    temas_texto = [
        "convide o seguidor pra um conteúdo exclusivo, com desejo e intimidade",
        "faça uma provocação sexual direta, como se estivesse mandando mensagem pro seguidor",
        "fale sobre o que você vai fazer hoje à noite, com insinuação",
        "faça o seguidor sentir que está perdendo algo imperdível",
        "mande um recado safado como se fosse de manhã acordando",
        "fale sobre uma fantasia sexual de forma direta e convidativa",
        "faça uma chacoalhada no seguidor, diga que ele precisa te ver agora",
    ]
    temas_enquete = [
        "O que você quer ver de mim?",
        "Como você prefere que eu apareça?",
        "Qual é sua maior fantasia comigo?",
        "O que te deixa mais louco em mim?",
        "O que você faria se estivesse aqui agora?",
    ]

    import random
    if post_type == "poll":
        tema = random.choice(temas_enquete)
        prompt = (
            f"Crie uma enquete para canal Telegram adulto com o tema: '{tema}'\n"
            f"Primeira linha = a pergunta (curta, direta, no estilo da persona abaixo).\n"
            f"Próximas 3-4 linhas = opções de resposta (curtas, safadas, brasileiras).\n"
            f"Use gírias brasileiras naturais. Sem palavras como 'exótico', 'sensual', 'provocante', 'único'.\n"
            f"Persona de referência (tom, não repita literalmente): {persona[:300]}"
            f"{historico}"
        )
    else:
        tema = random.choice(temas_texto)
        prompt = (
            f"Escreva uma mensagem curta para canal Telegram adulto: {tema}.\n"
            f"Escreva na primeira pessoa, como se fosse a própria modelo falando.\n"
            f"Use linguagem coloquial brasileira, gírias naturais. "
            f"Sem palavras pomposas. Máx 2 frases.\n"
            f"Persona de referência (tom, não repita literalmente): {persona[:300]}"
            f"{historico}"
        )

    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {xai_key}", "Content-Type": "application/json"},
            json={
                "model": "grok-4.3",
                "messages": [
                    {"role": "system", "content": "Você é uma modelo brasileira adulta que escreve posts para canal Telegram. Escreva APENAS o texto do post, sem explicações, sem aspas, sem introdução."},
                    {"role": "user",   "content": prompt},
                ],
                "max_tokens": 200,
                "temperature": 0.95,
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


def generate_cta_label(msg, stype, xai_key):
    """Gera texto curto, explícito e contextualizado para o botão CTA."""
    if not xai_key or not msg or stype == "poll":
        return ""
    try:
        prompt = (
            f"Post de canal adulto no Telegram: \"{msg[:200]}\"\n\n"
            f"Escreva o texto de um botão CTA para esse post. Regras:\n"
            f"- Máximo 20 caracteres\n"
            f"- Explícito, safado, direto\n"
            f"- Contextualizado com o que está no post\n"
            f"- 1 emoji + texto curtíssimo\n"
            f"- Exemplos: '🍆 Ver completo', '🔞 Quero mais', '💦 Assiste aqui', '😈 Entra no grupo'\n"
            f"Responda SOMENTE com o texto do botão, sem aspas, sem explicação."
        )
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {xai_key}", "Content-Type": "application/json"},
            json={
                "model": "grok-4.3",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 15,
                "temperature": 0.9,
            },
            timeout=15,
        )
        data = r.json()
        if r.ok:
            return data["choices"][0]["message"]["content"].strip().strip('"\'')[:30]
        return ""
    except Exception:
        return ""


def generate_copy_vision(persona, image_url, xai_key, used_texts=None):
    if not xai_key:
        return "", "Chave Grok/xAI não informada"
    try:
        img_resp = requests.get(image_url, timeout=15)
        img_resp.raise_for_status()
        content_type = img_resp.headers.get("content-type", "image/jpeg").split(";")[0]
        img_b64 = base64.b64encode(img_resp.content).decode("utf-8")
        img_data_url = f"data:{content_type};base64,{img_b64}"
    except Exception as e:
        print(f"[Vision] Erro ao baixar imagem: {e}")
        return "", f"Erro ao baixar imagem: {e}"
    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {xai_key}", "Content-Type": "application/json"},
            json={
                "model": "grok-4.3",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": img_data_url}},
                        {"type": "text", "text": (
                            "Olha essa foto e escreve uma legenda curta pra canal Telegram adulto, "
                            "falando exatamente o que tá acontecendo na imagem. "
                            "Escreve na primeira pessoa, como se você fosse a gostosa da foto. "
                            "Usa linguagem coloquial brasileira, gírias naturais, jeito de falar de mulher safada mesmo. "
                            "Sem palavras chatas como 'exótico', 'sensual', 'provocante'. "
                            "Máximo 2 frases. Só o texto, sem explicação."
                        )},
                    ],
                }],
                "max_tokens": 250,
                "temperature": 0.95,
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
            return "", f"xAI Vision HTTP {r.status_code}: {err}"
        msg = data["choices"][0]["message"]["content"].strip()
        print(f"[Vision] OK: {msg[:80]}")
        return msg, ""
    except Exception as e:
        print(f"[Vision] Exception: {e}")
        return "", str(e)

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
    today    = datetime.now().strftime("%Y-%m-%d")
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
    campaigns = load_data()
    for c in campaigns:
        if c["id"] == campaign_id:
            c.setdefault("logs", []).insert(0, {
                "slot_id": slot_id,
                "time":    datetime.now().isoformat(),
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
                used = [s.get("media_path","") for s in day_data.get("slots",[]) if s.get("media_path")]
                posted_by_day[day] = set(used)
    total_bytes = 0
    days_result = []
    root_items  = real_sb.storage.from_(BUCKET).list() or []
    day_folders = [item.get("name") for item in root_items if item.get("metadata") is None and item.get("name")]
    for day in sorted(day_folders, reverse=True):
        if campaign_days_filter is not None and day not in campaign_days_filter:
            continue
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
        if stype in ("image", "video") and photo_idx < len(photos):
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
    if not xai_key:
        return jsonify({"ok": False, "error": "Chave Grok não informada"})
    vision_err = ""
    msg = ""
    if stype in ("image", "video") and media_path:
        msg, vision_err = generate_copy_vision(persona, media_path, xai_key)
        if not msg:
            print(f"[Vision] Falhou ({vision_err}), usando fallback texto")
            msg, _ = generate_copy(persona, stype, xai_key)
    else:
        msg, vision_err = generate_copy(persona, stype, xai_key)
    cta_label = generate_cta_label(msg, stype, xai_key) if msg else ""
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
            msg, _ = generate_copy_vision(persona, cfg["media_path"], xai_key)
        else:
            msg, _ = generate_copy(persona, stype, xai_key)
        cta = generate_cta_label(msg, stype, xai_key) if msg else ""
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
