#!/usr/bin/env bash
# Instalador de Fuji: cara ASCII en la terminal conectada a tu Claude Code.
# Uso: ./install.sh        (no necesita root salvo para paquetes del sistema, y pregunta antes)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${XDG_DATA_HOME:-$HOME/.local/share}/fuji"
BIN="$HOME/.local/bin"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/fujibot"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*"; exit 1; }
ask()  { local r; read -rp "  $1 [s/N] " r; [[ "$r" =~ ^[sSyY]$ ]]; }

bold "Fuji · instalador"

# ── 1. Requisitos básicos ───────────────────────────────────────────
command -v python3 >/dev/null || die "Necesitas python3"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Necesitas Python 3.9 o superior"
python3 -c 'import venv, curses' 2>/dev/null || die "Tu Python no trae venv/curses (en Debian/Ubuntu: sudo apt install python3-venv)"
ok "Python $(python3 -c 'import platform; print(platform.python_version())')"

# ── 2. Claude Code ──────────────────────────────────────────────────
if command -v claude >/dev/null || [[ -x "$HOME/.local/bin/claude" ]]; then
    ok "Claude Code encontrado"
else
    warn "No encuentro Claude Code (el comando 'claude')."
    echo "    Instálalo con el instalador oficial:"
    echo "      curl -fsSL https://claude.ai/install.sh | bash"
    echo "    y luego ejecuta 'claude' una vez para iniciar sesión."
    ask "¿Sigo con la instalación de Fuji igualmente?" || exit 1
fi

# ── 3. Paquetes del sistema (voz y portapapeles, opcionales) ────────
VOICE=0
if ask "¿Instalar también la voz? (micrófono + voz hablada, descarga ~500 MB)"; then
    VOICE=1
fi
PKGS=()
if command -v pacman >/dev/null; then
    PKGS=(espeak-ng wl-clipboard); ((VOICE)) && PKGS+=(portaudio)
    PM=(sudo pacman -S --needed)
elif command -v apt-get >/dev/null; then
    PKGS=(espeak-ng wl-clipboard); ((VOICE)) && PKGS+=(libportaudio2)
    PM=(sudo apt-get install -y)
elif command -v dnf >/dev/null; then
    PKGS=(espeak-ng wl-clipboard); ((VOICE)) && PKGS+=(portaudio)
    PM=(sudo dnf install -y)
fi
if ((${#PKGS[@]})); then
    echo "  Paquetes del sistema recomendados: ${PKGS[*]}"
    if ask "¿Instalarlos ahora con '${PM[*]}'?"; then
        "${PM[@]}" "${PKGS[@]}"
    else
        warn "Sin ellos Fuji funciona, pero sin voz hablada (espeak-ng) ni pegar del portapapeles (wl-clipboard)"
    fi
else
    warn "No reconozco tu gestor de paquetes: instala a mano espeak-ng y wl-clipboard (y portaudio para la voz)"
fi

# ── 4. Entorno de Python propio (no toca tu sistema) ────────────────
mkdir -p "$APP" "$BIN"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet -r "$SRC/requirements.txt"
((VOICE)) && "$APP/venv/bin/pip" install --quiet -r "$SRC/requirements-voice.txt"
ok "Dependencias de Python en $APP/venv"

install -m 644 "$SRC/fujibot.py" "$APP/fujibot.py"
cat > "$BIN/fuji" <<LAUNCHER
#!/bin/sh
exec "$APP/venv/bin/python" "$APP/fujibot.py" "\$@"
LAUNCHER
chmod +x "$BIN/fuji"
ok "Comando 'fuji' en $BIN/fuji"

# ── 5. Configuración (nunca se sobrescribe la existente) ────────────
mkdir -p "$CONF"
if [[ -f "$CONF/env" ]]; then
    ok "Configuración existente conservada: $CONF/env"
else
    install -m 600 "$SRC/env.example" "$CONF/env"
    ok "Configuración creada: $CONF/env (solo legible por ti)"
fi

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) warn "$BIN no está en tu PATH: añádelo a tu shell, p. ej. export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

bold "Listo. Ejecuta: fuji"
