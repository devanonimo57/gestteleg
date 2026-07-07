#!/bin/bash
DEST="$HOME/Documents/win/Yago/MODELO VIT/CLAUDE GERA/Geração 003"
mkdir -p "$DEST"
mv "$HOME/Downloads/geracao_003_variacao_"*.png "$DEST/" 2>/dev/null
echo "Pronto! $(ls "$DEST" | wc -l) imagens na pasta."
read -p "Pressione Enter para fechar..."
