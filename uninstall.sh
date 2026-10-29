#!/usr/bin/env bash
# Desinstala Fuji. Tu configuración (~/.config/fujibot) solo se borra si lo confirmas.
set -euo pipefail
APP="${XDG_DATA_HOME:-$HOME/.local/share}/fuji"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/fujibot"
rm -rf "$APP" "$HOME/.local/bin/fuji"
echo "Fuji desinstalado."
read -rp "¿Borrar también tu configuración y claves en $CONF? [s/N] " r
[[ "$r" =~ ^[sSyY]$ ]] && rm -rf "$CONF" && echo "Configuración borrada."
exit 0
