#!/bin/bash
# Geração 003 — Mulher Carioca (Pinterest ref)
DEST="$HOME/Documents/win/Yago/MODELO VIT/CLAUDE GERA/Geração 003"
mkdir -p "$DEST"

declare -a URLS=(
  "https://pikaso.cdnpk.net/private/production/4740108943/render.png?token=exp=1783036800~hmac=fe2525f7dac0d4df8cdf56080fbc55bc2caebacf9bc7d2bafc842ae28a048d7e"
  "https://pikaso.cdnpk.net/private/production/4740108319/render.png?token=exp=1783036800~hmac=09f42e9bda0f0c899b197f433b2adb44395a5ff335be6b606d63c10f2a1092b7"
  "https://pikaso.cdnpk.net/private/production/4740108987/render.png?token=exp=1783036800~hmac=881cca1ce69be422879e609ae2c8677c5651a58400d0aee29cf02a9c09055105"
  "https://pikaso.cdnpk.net/private/production/4740112008/render.png?token=exp=1783036800~hmac=a1fb3c0442d60c6b1127221fb27c40df9d445e85db26ea24eab303710e0ece78"
  "https://pikaso.cdnpk.net/private/production/4740111471/render.png?token=exp=1783036800~hmac=aa9c37b26b1474f58b4ee391efd0091308036b146887720a9fb08f5a0a5656ab"
)

for i in "${!URLS[@]}"; do
  NUM=$(printf "%02d" $((i+1)))
  FILE="$DEST/variacao_${NUM}.png"
  echo -n "Baixando variacao_${NUM}.png... "
  curl -sL "${URLS[$i]}" -o "$FILE"
  SIZE=$(du -h "$FILE" | cut -f1)
  echo "✓ $SIZE"
done

echo ""
echo "✅ 5 imagens salvas em:"
echo "   $DEST"
