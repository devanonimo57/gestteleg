#!/usr/bin/env python3
"""
censor_local.py — Censura fotos de um dia usando NudeNet localmente no Mac.

USO:
    python censor_local.py <dia>  [--emoji <nome>] [--bucket <nome>]

    <dia>      Data no formato YYYY-MM-DD  (ex: 2026-07-08)
    --emoji    fire | devil | 18 | drool | flower  (padrão: fire)
    --bucket   nome do bucket Supabase       (padrão: media)

VARIÁVEIS DE AMBIENTE NECESSÁRIAS:
    SUPABASE_URL   — URL do projeto Supabase
    SUPABASE_KEY   — chave service_role ou anon do Supabase

INSTALAÇÃO DE DEPENDÊNCIAS (só na primeira vez):
    pip install nudenet pillow supabase requests

EXEMPLO:
    SUPABASE_URL=https://xxx.supabase.co SUPABASE_KEY=eyJ... python censor_local.py 2026-07-08
    ou adicione as variáveis no seu ~/.zshrc / .env e rode:
    python censor_local.py 2026-07-08 --emoji devil
"""

import sys
import os
import io
import gc
import tempfile
import argparse
import random
import requests

# ─── Configurações ────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Pasta onde estão os PNGs de emoji (relativo a este script)
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
EMOJI_FILES  = {
    "fire":   os.path.join(SCRIPT_DIR, "static", "emojis", "fire.png"),
    "devil":  os.path.join(SCRIPT_DIR, "static", "emojis", "devil.png"),
    "18":     os.path.join(SCRIPT_DIR, "static", "emojis", "18.png"),
    "drool":  os.path.join(SCRIPT_DIR, "static", "emojis", "drool.png"),
    "flower": os.path.join(SCRIPT_DIR, "static", "emojis", "flower.png"),
}

TARGET_CLASSES = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "FEMALE_NIPPLE_EXPOSED",
}

BREAST_CLASSES = {"FEMALE_BREAST_EXPOSED", "FEMALE_NIPPLE_EXPOSED"}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def check_env():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("❌  SUPABASE_URL e SUPABASE_KEY precisam estar definidos como variáveis de ambiente.")
        print("    Exemplo:")
        print("    export SUPABASE_URL=https://xxx.supabase.co")
        print("    export SUPABASE_KEY=eyJ...")
        sys.exit(1)


def get_supabase():
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def load_emoji(name: str, size: int):
    from PIL import Image
    path = EMOJI_FILES.get(name, EMOJI_FILES["fire"])
    if not os.path.exists(path):
        print(f"⚠️  Emoji não encontrado: {path} — usando fire.png")
        path = EMOJI_FILES["fire"]
    return Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)


def detect_areas(image_bytes: bytes) -> list[dict]:
    """Detecta partes explícitas usando NudeNet. Retorna lista de {x,y,r} em frações da imagem."""
    from nudenet import NudeDetector
    from PIL import Image as PILImage

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name

    try:
        detector = NudeDetector()
        detections = detector.detect(tmp_path)
        del detector
        gc.collect()
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    img = PILImage.open(io.BytesIO(image_bytes))
    w, h = img.size

    areas = []
    for d in detections:
        cls = d.get("class")
        if cls not in TARGET_CLASSES:
            continue
        if d.get("score", 0) < 0.3:
            continue
        box = d.get("box", [])
        if len(box) < 4:
            continue
        bx, by, bw, bh = box
        cx = (bx + bw / 2) / w
        cy = (by + bh / 2) / h
        factor = 0.33 if cls in BREAST_CLASSES else 0.45
        r = min(bw, bh) * factor / w
        areas.append({"x": cx, "y": cy, "r": r})

    return areas


def apply_emojis(image_bytes: bytes, areas: list[dict], emoji_name: str) -> bytes:
    """Sobrepõe emojis nas áreas e retorna JPEG bytes."""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGBA")
    w, h = img.size

    for area in areas:
        x_frac = float(area["x"])
        y_frac = float(area["y"])
        r_frac = float(area["r"])
        size   = max(70, int(r_frac * w * 1.8))
        emoji  = load_emoji(emoji_name, size)
        cx = int(x_frac * w) - size // 2
        cy = int(y_frac * h) - size // 2
        cx = max(0, min(cx, w - size))
        cy = max(0, min(cy, h - size))
        img.paste(emoji, (cx, cy), emoji)

    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ─── Principal ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Censura fotos de um dia via NudeNet local")
    parser.add_argument("day",    help="Data no formato YYYY-MM-DD")
    parser.add_argument("--emoji",  default="fire",  choices=list(EMOJI_FILES.keys()), help="Emoji a usar (padrão: fire)")
    parser.add_argument("--bucket", default="media", help="Nome do bucket Supabase (padrão: media)")
    parser.add_argument("--random-emoji", action="store_true", help="Usa emoji aleatório por foto")
    args = parser.parse_args()

    check_env()
    sb = get_supabase()

    # Lista arquivos do dia
    print(f"\n📂  Buscando fotos em /{args.bucket}/{args.day}/ ...")
    try:
        files = sb.storage.from_(args.bucket).list(path=args.day) or []
    except Exception as e:
        print(f"❌  Erro ao listar: {e}")
        sys.exit(1)

    photos = [f for f in files if f.get("name") and not f["name"].startswith(".")]
    if not photos:
        print("⚠️  Nenhuma foto encontrada neste dia.")
        sys.exit(0)

    print(f"✅  {len(photos)} foto(s) encontrada(s).\n")

    ok = err = skip = 0
    for f in photos:
        fname     = f["name"]
        full_path = f"{args.day}/{fname}"
        public_url = sb.storage.from_(args.bucket).get_public_url(full_path)

        emoji_name = random.choice(list(EMOJI_FILES.keys())) if args.random_emoji else args.emoji
        print(f"  🔍 {fname}  [{emoji_name}]", end="  ", flush=True)

        try:
            # Download
            resp = requests.get(public_url, timeout=30)
            resp.raise_for_status()
            img_bytes = resp.content

            # Detecção
            areas = detect_areas(img_bytes)
            if not areas:
                print("sem áreas detectadas — pulando")
                skip += 1
                continue

            # Aplicação
            censored = apply_emojis(img_bytes, areas, emoji_name)

            # Upload (substitui original)
            sb.storage.from_(args.bucket).remove([full_path])
            sb.storage.from_(args.bucket).upload(
                path=full_path,
                file=censored,
                file_options={"content-type": "image/jpeg", "upsert": "true"},
            )
            print(f"✅ {len(areas)} área(s) censurada(s)")
            ok += 1

        except Exception as e:
            print(f"❌ erro: {e}")
            err += 1

    print(f"\n─── Resultado ───────────────────────────────")
    print(f"  Censuradas : {ok}")
    print(f"  Puladas    : {skip}  (sem áreas explícitas)")
    print(f"  Erros      : {err}")
    print(f"─────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
