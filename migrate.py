"""
migrate.py — Importa o campaigns.json local para o Supabase.
Execute UMA VEZ antes de subir para o Render:

    pip install supabase
    set SUPABASE_URL=https://xxx.supabase.co
    set SUPABASE_SERVICE_KEY=eyJ...
    python migrate.py
"""
import json, os, sys
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Defina as variáveis SUPABASE_URL e SUPABASE_SERVICE_KEY antes de rodar.")
    sys.exit(1)

if not os.path.exists("campaigns.json"):
    print("ℹ️  campaigns.json não encontrado — nada a migrar.")
    sys.exit(0)

with open("campaigns.json", encoding="utf-8") as f:
    campaigns = json.load(f)

if not campaigns:
    print("ℹ️  campaigns.json está vazio — nada a migrar.")
    sys.exit(0)

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

for c in campaigns:
    sb.table("campaigns").upsert({"id": c["id"], "data": c}).execute()
    print(f"✅ Migrado: {c.get('name', '?')} ({c['id'][:8]}...)")

print(f"\n🎉 {len(campaigns)} campanha(s) importada(s) com sucesso!")
