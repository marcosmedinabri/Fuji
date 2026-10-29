#!/usr/bin/env python3
"""
Fuji — cara ASCII en terminal sobre lluvia estilo cmatrix.
Parpadea, mueve la boca sincronizada con la voz y responde por voz.

  T/Enter  escribir una pregunta (Enter envía, Esc cancela)
  ESPACIO  voz: empezar/terminar de hablar · mientras responde: mostrar todo
  r        olvidar la conversación
  q        salir

Pipeline: micrófono -> faster-whisper (STT local) -> comandos locales o LLM
(Ollama local o API de Anthropic) -> TTS (espeak-ng o piper) con lip-sync.
"""
import curses
import json
import locale
import math
import base64
import os
import queue
import random
import shutil
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import wave

import numpy as np

try:
    import sounddevice as sd
except Exception:  # sin PortAudio o sin paquete
    sd = None

# ───────────────────────── configuración (vía variables de entorno) ─────────────────────────
def load_env_file(path=os.path.expanduser("~/.config/fujibot/env")):
    """Carga KEY=VALUE de ~/.config/fujibot/env (secretos fuera del código). El entorno real manda."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except FileNotFoundError:
        pass

load_env_file()

# comandos de Bash que se ejecutan sin pedir permiso (coincidencia exacta),
# separados por comas en FUJI_TRUSTED_COMMANDS; por defecto ninguno
TRUSTED_COMMANDS = {c.strip() for c in os.getenv("FUJI_TRUSTED_COMMANDS", "").split(",") if c.strip()}
TRUSTED_COMMANDS |= {os.path.expanduser(c) for c in TRUSTED_COMMANDS}


def claude_memory_dir(cwd):
    """Carpeta de memoria de Claude Code para un directorio (misma regla que usa el CLI)."""
    project = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
    return os.path.join(os.path.expanduser("~/.claude/projects"), project, "memory")

CFG = {
    "backend": os.getenv("CARA_BACKEND", "claude"),             # claude | groq | ollama | anthropic
    "groq_api_key": os.getenv("GROQ_API_KEY", ""),              # en ~/.config/fujibot/env
    "groq_model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
    "type_cps": float(os.getenv("CARA_CPS", "35")),              # letras por segundo al escribir
    "ollama_url": os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat"),
    "ollama_model": os.getenv("OLLAMA_MODEL", "llama3.2"),
    "anthropic_model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
    # backend "claude": Claude Code en modo no interactivo, con tu sesión y tu memoria
    # sin depender del PATH: al lanzarlo desde Hyprland no se carga el .zshrc que añade ~/.local/bin
    "claude_bin": os.getenv("CLAUDE_BIN") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude"),
    "claude_cwd": os.path.expanduser(os.getenv("CLAUDE_CWD", "~")),    # su memoria va ligada a este directorio
    "claude_effort": os.getenv("CLAUDE_EFFORT", "low"),                # low = respuestas más rápidas
    # única carpeta donde puede escribir: la memoria persistente (compartida con Claude Code)
    "claude_memory": os.getenv("CLAUDE_MEMORY") or claude_memory_dir(os.path.expanduser(os.getenv("CLAUDE_CWD", "~"))),
    "claude_system": ("El usuario te habla por voz o por teclado a través de Fuji, una cara ASCII "
                      "en su terminal. Responde en español, breve (máximo 4 frases), en texto plano, "
                      "sin markdown ni emojis: tu respuesta se lee en voz alta y se escribe letra a letra. "
                      "Puedes leer archivos y ejecutar comandos de Bash: el usuario verá cada comando encima "
                      "de tu cabeza y lo aprobará o denegará; si lo deniega, no insistas y responde sin él. "
                      "Fuera de tu carpeta de memoria persistente no escribas archivos salvo que te lo pida. "
                      "Cuando te pida que recuerdes algo, o aprendas algo duradero sobre él, guárdalo en la "
                      "memoria siguiendo tus instrucciones de memoria."),
    # cuándo vuelve a haber uso de Claude tras agotarlo (sobrevive a reinicios de Fuji)
    "sleep_file": os.path.expanduser("~/.cache/fujibot-sleep"),
    "whisper_model": os.getenv("WHISPER_MODEL", "small"),       # tiny|base|small|medium
    "whisper_device": os.getenv("WHISPER_DEVICE", "cpu"),       # cpu | cuda
    "tts": os.getenv("CARA_TTS", "espeak"),                     # espeak | piper
    "espeak_voice": os.getenv("ESPEAK_VOICE", "es"),
    "piper_bin": os.getenv("PIPER_BIN", "piper"),
    "piper_model": os.getenv("PIPER_MODEL", ""),                # ruta a es_ES-*.onnx
    "face": "macos",                                            # cara única de Macintosh
    "katakana": os.getenv("CARA_KATAKANA", "0") == "1",
    "rate": 16000,
    "system": ("Eres una cara ASCII que vive en una terminal Linux. Respondes en español, "
               "de forma breve (máximo 4 frases) y en texto plano, sin markdown ni emojis, "
               "porque tu respuesta se muestra letra a letra en la terminal."),
}
LOG = os.path.expanduser("~/.cache/carabot.log")

# ───────────────────────── estado compartido ─────────────────────────
class State:
    def __init__(self):
        self.mode = "idle"          # idle | listening | transcribing | thinking | speaking
        self.level = 0.0            # amplitud de voz 0..1 para la boca
        self.heard = ""
        self.reply = "                Pulsa T o Enter para escribirme."
        self.history = []
        self.running = True
        self.interrupt = threading.Event()
        self.typing = False         # caja de texto abierta
        self.buf = ""               # lo que estás escribiendo
        self.shown = None           # letras visibles de la respuesta (None = todas)
        self.running_after_speech = True
        self.thought = None         # acción que está "pensando" (bocadillo sobre la cabeza)
        self.thought_until = 0.0    # hasta cuándo se ve el bocadillo
        self.perm = None            # permiso pendiente: {"text", "event", "allow"}
        self.sleep_until = None     # epoch en que vuelve a haber uso de Claude
        self.transcript = []        # historial completo de la sesión: (rol, texto, hora)
        self.show_history = False   # panel de historial abierto (tecla H)
        self.hist_scroll = 0        # líneas desplazadas desde el final
        self.need_clear = False     # borrar la pantalla en el próximo fotograma
        self.attachments = []       # imágenes pegadas en el cuadro de texto: {"media_type", "data", "size"}
        self.input_note = ""        # aviso breve en el cuadro de texto (p. ej. "portapapeles vacío")

S = State()

def tlog(role, text):
    """Apunta en el historial visible (user | assistant | tool | system)."""
    S.transcript.append((role, text, time.strftime("%H:%M")))

# ───────────────────────── bocadillo de pensamiento y permisos ─────────────────────────
THOUGHT_MIN = 1.5  # segundos mínimos que se ve cada pensamiento

def _short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and path.startswith(home) else (path or "")

def describe_tool(name, inp):
    inp = inp or {}
    if name == "Bash":
        return "$ " + inp.get("command", "")
    if name == "Read":
        return "leyendo " + _short_path(inp.get("file_path", ""))
    if name == "Grep":
        return f"buscando «{inp.get('pattern', '')}»" + (f" en {_short_path(inp['path'])}" if inp.get("path") else "")
    if name == "Glob":
        return "buscando archivos " + inp.get("pattern", "")
    if name in ("Write", "Edit"):
        return "escribiendo " + _short_path(inp.get("file_path", ""))
    return f"{name} {json.dumps(inp, ensure_ascii=False)[:80]}"

def think(text):
    S.thought, S.thought_until = text, float("inf")

def stop_thinking():
    if S.thought and S.thought_until == float("inf"):
        S.thought_until = time.time() + THOUGHT_MIN

def ask_permission(text):
    """Muestra el comando en el bocadillo y espera a que el usuario pulse S o N."""
    req = {"text": text, "event": threading.Event(), "allow": False}
    think(text)
    if S.show_history:  # el permiso manda: cerrar el historial para ver el bocadillo
        S.show_history, S.need_clear = False, True
    S.perm = req
    req["event"].wait()
    S.perm = None
    tlog("tool", f"{text}  · {'permitido' if req['allow'] else 'denegado'}")
    return req["allow"]

def answer_permission(allow):
    req = S.perm
    if req:
        req["allow"] = allow
        req["event"].set()

# ───────────────────────── portapapeles: Ctrl+V y pegado del terminal ─────────────────────────
MAX_IMAGE_BYTES = 3_700_000  # ~5 MB en base64, el máximo que acepta la API por imagen
MAX_BUF = 20_000             # caracteres en el cuadro de texto (texto pegado incluido)

def _wl(*args):
    return subprocess.run(["wl-paste", *args], capture_output=True, timeout=5)

def paste_clipboard():
    """Ctrl+V: imagen → adjunto; texto → se inserta en el cuadro."""
    try:
        types = _wl("--list-types").stdout.decode(errors="replace").split()
    except FileNotFoundError:
        S.input_note = "falta wl-clipboard (wl-paste)"
        return
    except subprocess.TimeoutExpired:
        S.input_note = "el portapapeles no responde"
        return
    img = next((t for t in ("image/png", "image/jpeg", "image/webp", "image/gif") if t in types), None)
    if img:
        data = _wl("--no-newline", "-t", img).stdout
        if not data:
            S.input_note = "no pude leer la imagen"
        elif len(data) > MAX_IMAGE_BYTES:
            S.input_note = f"imagen demasiado grande ({len(data) / 1e6:.1f} MB, máx. 3,7 MB)"
        else:
            S.attachments.append({"media_type": img, "data": base64.b64encode(data).decode(),
                                  "size": len(data)})
            S.input_note = ""
        return
    text = _wl("--no-newline").stdout.decode(errors="replace")
    if text:
        insert_text(text)
    else:
        S.input_note = "portapapeles vacío"

def insert_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    text = "".join(c for c in text if c == "\n" or c.isprintable())
    room = MAX_BUF - len(S.buf)
    if len(text) > room:
        text, S.input_note = text[:room], "texto recortado: demasiado largo"
    S.buf += text

def read_bracketed_paste(scr):
    """Tras un Esc, mira si llega "[200~" (pegado del terminal, Ctrl+Shift+V) y, si es así,
    lee hasta "\x1b[201~". Devuelve el texto pegado, o None si era un Esc normal."""
    def nxt(wait):
        end = time.time() + wait
        while time.time() < end:
            try:
                k = scr.get_wch()
            except curses.error:
                time.sleep(0.002)
                continue
            return k
        return None
    head = ""
    for _ in range(5):
        k = nxt(0.03)
        if not isinstance(k, str):
            return None
        head += k
        if not "[200~".startswith(head):
            return None
    buf = ""
    while True:
        k = nxt(1.0)
        if k is None:  # el terminal no cerró el pegado: quedarse con lo recibido
            break
        if isinstance(k, str):
            buf += k
            if buf.endswith("\x1b[201~"):
                buf = buf[:-6]
                break
    return buf

# ───────────────────────── límite de uso: Fuji "duerme" ─────────────────────────
class Sleeping(Exception):
    pass

def go_to_sleep(until):
    S.sleep_until = until
    try:
        with open(CFG["sleep_file"], "w") as f:
            f.write(str(int(until)) if until else "")
    except OSError:
        pass

def wake_up():
    S.sleep_until = None
    try:
        os.remove(CFG["sleep_file"])
    except OSError:
        pass

def load_sleep():
    try:
        with open(CFG["sleep_file"]) as f:
            until = float(f.read().strip() or 0)
        if until > time.time():
            S.sleep_until = until
        else:
            wake_up()
    except (OSError, ValueError):
        pass

def sleeping():
    if S.sleep_until and time.time() >= S.sleep_until:
        wake_up()
        S.reply, S.shown = "Ya he descansado. Vuelvo a estar aquí.", None
        if CFG["backend"] == "claude":
            CLAUDE.warm()
    return S.sleep_until is not None

def fmt_left(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s_ = divmod(rem, 60)
    return f"{h}h {m:02d}m {s_:02d}s" if h else f"{m}m {s_:02d}s"

def log(msg):
    with open(LOG, "a") as f:
        f.write(time.strftime("%F %T ") + str(msg) + "\n")

# ───────────────────────── la cara de Macintosh ─────────────────────────
# El lienzo se define en "pixeles" lógicos. Cada pixel ocupa dos columnas
# de terminal, así que el marco debe usar el ancho renderizado, no FW.
# Cara más compacta, manteniendo la proporción de la referencia.
FW, FH = 13, 10
INNER_W = FW * 2
# Las filas de la cara se dibujan como "│ " + contenido + " │".
FRAME_W = INNER_W + 4
BASE_W = 23
BASE_LEFT_PAD = (FRAME_W - BASE_W) // 2
BASE_RIGHT_PAD = FRAME_W - BASE_W - BASE_LEFT_PAD

MOUTHS = {
    "flat": [".....", ".....", "....."],
    "oh":   [".###.", ".#.#.", ".###."],
    "t0":   [".....", ".###.", "....."],
    "t1":   [".....", ".###.", ".#.#."],
    "t2":   [".###.", "#...#", ".###."],
    "t3":   ["#####", "#...#", "#####"],
    "smile": [".....", "#...#", ".###."],
}

FACES = {
    "macos": {
        "eyes": {
            "open":   [".#.", ".#."],
            "half":   ["...", ".#."],
            "closed": ["...", "..."],
            "up":     [".#.", "..."],
            "left":   ["...", "##."],
            "right":  ["...", ".##"],
        },
        # Posiciones en el lienzo lógico de 13x10.
        "eye_pos": [(2, 1), (9, 1)],
        "nose": [".#.", ".#.", ".#."],
        "nose_pos": (6, 1),
        "mouth_y": 5,
        "idle": [".###."],

        # Marco de pantalla: todas las líneas tienen exactamente FRAME_W.
        "top": [
            "┌" + "─" * (FRAME_W - 2) + "┐",
            "│ " + " " * INNER_W + " │",
        ],
        "screen_top": [
            "├" + "─" * (FRAME_W - 2) + "┤",
        ],
        # Cuerpo inferior de la carcasa, alineado exactamente con la pantalla.
        "bottom": [
            "│ " + " " * INNER_W + " │",
            "│ " + " " * 4 + "━" * 3 + " " * 11 + "━" * 4 + " " * 4 + " │",
            "│ " + " " * INNER_W + " │",
            "└" + "─" * (FRAME_W - 2) + "┘",
            " " * BASE_LEFT_PAD + "┌" + "─" * (BASE_W - 2) + "┐" + " " * BASE_RIGHT_PAD,
            " " * BASE_LEFT_PAD + "└" + "─" * (BASE_W - 2) + "┘" + " " * BASE_RIGHT_PAD,
        ],
    }
}

def _px(row):
    return "".join("██" if c == "#" else "  " for c in row)

def build_face(eye, mouth_key, name=None):
    f = FACES["macos"]
    g = [["."] * FW for _ in range(FH)]

    def blit(spr, x, y):
        for j, row in enumerate(spr):
            for i, c in enumerate(row):
                if c == "#" and 0 <= y + j < FH and 0 <= x + i < FW:
                    g[y + j][x + i] = "#"

    # Pintar ojos
    for x, y in f["eye_pos"]:
        blit(f["eyes"][eye], x, y)
    
    # Pintar nariz
    nx, ny = f["nose_pos"]
    blit(f["nose"], nx, ny)

    # Pintar boca
    mouth_spr = f["idle"] if mouth_key == "idle" else MOUTHS[mouth_key]
    blit(mouth_spr, (FW - 5) // 2, f.get("mouth_y", 5))

    out = list(f["top"])
    out += ["│ " + _px(r) + " │" for r in g]
    out += f["screen_top"]
    out += f["bottom"]
    return out

def now_breath():
    return time.time() / 1.6

class Anim:
    def __init__(self):
        self.next_blink = time.time() + random.uniform(2, 5)
        self.blink_t = None
        self.mouth_v = 0.0
        self.trick = None           # travesura en curso: ball | nap | look
        self.trick_t = 0.0
        self.next_trick = time.time() + random.uniform(90, 240)

    TRICKS = {"ball": 9, "nap": 18, "look": 5}  # duración en segundos

    def update_trick(self, now, mode):
        """De vez en cuando, estando en espera, hace algo por su cuenta."""
        if mode != "idle":
            if self.trick:
                self.trick = None
                S.need_clear = True
            self.next_trick = max(self.next_trick, now + random.uniform(60, 120))
            return None
        if self.trick and now - self.trick_t > self.TRICKS[self.trick]:
            self.trick = None
            S.need_clear = True
            self.next_trick = now + random.uniform(120, 360)
        elif not self.trick and now >= self.next_trick:
            self.trick, self.trick_t = random.choice(list(self.TRICKS)), now
        return self.trick

    def ball_pos(self, now):
        """Pelota botando de lado a lado: (dx desde el centro, altura sobre la cabeza)."""
        t = now - self.trick_t
        dx = int(round(10 * math.sin(t * 0.9)))
        hgt = int(abs(math.sin(t * 3.2)) * 5)
        return dx, hgt

    def eye(self, now, mode):
        if mode == "sleeping" or self.trick == "nap":
            return "closed"
        if self.trick == "ball":
            dx, hgt = self.ball_pos(now)
            return "left" if dx < -3 else "right" if dx > 3 else "up"
        if self.trick == "look":
            t = now - self.trick_t
            return "left" if t < 1.5 else "right" if t < 3 else "up"
        if self.blink_t is None and now >= self.next_blink:
            self.blink_t = now
        if self.blink_t is not None:
            dt = now - self.blink_t
            if dt < 0.05:
                return "half"
            if dt < 0.13:
                return "closed"
            if dt < 0.18:
                return "half"
            self.blink_t = None
            self.next_blink = now + (0.25 if random.random() < 0.15 else random.uniform(2.5, 6))
        if mode == "sleeping":
            return "closed"
        if mode in ("listening", "asking"):
            return "up"
        if mode in ("transcribing", "thinking"):
            return "left" if int(now * 2.5) % 2 else "right"
        return "open"

    def mouth(self, mode, level):
        self.mouth_v += (level - self.mouth_v) * 0.6
        if mode == "speaking":
            v = self.mouth_v
            return "t0" if v < 0.12 else "t1" if v < 0.4 else "t2" if v < 0.75 else "t3"
        if mode == "listening":
            return "oh"
        if mode == "sleeping" or self.trick == "nap":
            return "t0" if int(now_breath()) % 2 else "flat"  # respira despacio
        if self.trick == "ball":
            return "smile"
        if self.trick == "look":
            return "oh"  # silbando
        if mode in ("transcribing", "thinking", "asking"):
            return "flat"
        return "idle"

# ───────────────────────── lluvia estilo cmatrix ─────────────────────────
ASCII_CH = "0123456789ABCDEFabcdef@#$%&*+=<>?/\\|"
KATA_CH = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789"

def put(scr, y, x, s, attr=0):
    h, w = scr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    if x < 0:
        s, x = s[-x:], 0
    s = s[: w - x - (1 if y == h - 1 else 0)]
    if not s:
        return
    try:
        scr.addstr(y, x, s, attr)
    except curses.error:
        pass

class Rain:
    def __init__(self, h, w):
        self.h, self.w = h, w
        self.chars = KATA_CH if CFG["katakana"] else ASCII_CH
        self.drops = [self._new(x, True) for x in range(0, w, 2)]

    def _new(self, x, anywhere=False):
        y = random.uniform(-self.h, 0) if anywhere else random.uniform(-self.h / 2, 0)
        return [x, y, random.uniform(0.25, 1.0), random.randint(4, max(5, self.h // 2))]

    def step(self, scr):
        green = curses.color_pair(1)
        head = curses.color_pair(2) | curses.A_BOLD
        for d in self.drops:
            x, y, sp, ln = d
            iy = int(y)
            put(scr, iy - 1, x, random.choice(self.chars), green)
            put(scr, iy, x, random.choice(self.chars), head)
            put(scr, iy - ln, x, " ")
            d[1] += sp
            if iy - ln > self.h:
                d[:] = self._new(x)

# ───────────────────────── audio: grabación ─────────────────────────
class Recorder:
    def __init__(self, rate):
        self.rate, self.frames, self.stream = rate, [], None

    def start(self):
        self.frames = []
        self.stream = sd.InputStream(samplerate=self.rate, channels=1, dtype="float32",
                                     callback=lambda d, f, t, st: self.frames.append(d.copy()))
        self.stream.start()

    def stop(self):
        self.stream.stop()
        self.stream.close()
        self.stream = None
        return np.concatenate(self.frames)[:, 0] if self.frames else np.zeros(0, np.float32)

REC = Recorder(CFG["rate"])

# ───────────────────────── STT ─────────────────────────
WHISPER = None
WHISPER_ERR = ""
WHISPER_READY = threading.Event()

def load_whisper():
    global WHISPER, WHISPER_ERR
    try:
        from faster_whisper import WhisperModel
        ct = "int8" if CFG["whisper_device"] == "cpu" else "float16"
        WHISPER = WhisperModel(CFG["whisper_model"], device=CFG["whisper_device"], compute_type=ct)
    except Exception as e:
        WHISPER_ERR = f"No se pudo cargar Whisper: {e}"
        log(WHISPER_ERR)
    finally:
        WHISPER_READY.set()

def transcribe(audio):
    WHISPER_READY.wait()
    if WHISPER is None:
        raise RuntimeError(WHISPER_ERR)
    segs, _ = WHISPER.transcribe(audio, language="es", vad_filter=True, beam_size=1)
    return " ".join(s.text.strip() for s in segs).strip()

# ───────────────────────── LLM ─────────────────────────
def http_json(url, body, headers, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json",
                                          "user-agent": "fuji/1.0", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            detail = detail[:200]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None

# A veces el modelo suelta una etiqueta interna (p. ej. "antml:reasoning_effort=5") en vez de
# responder. Se quita del principio del texto; mientras llega en streaming, no se muestra nada
# que aún pudiera ser el comienzo de una de esas etiquetas.
LEAK_RE = re.compile(r"^\s*antml:\S*\s*")

def clean_leak(text):
    head = text.lstrip()
    if head and "antml:".startswith(head):
        return ""
    return LEAK_RE.sub("", text)

class ClaudeSession:
    """Un proceso `claude -p` persistente (stream-json): se ahorra ~1 s de arranque por mensaje
    y la conversación sigue en la misma sesión. Las herramientas que usa aparecen como pensamientos
    y lo que necesita permiso (todo Bash, escrituras fuera de la memoria…) lo decide el usuario."""

    def __init__(self):
        self.proc = None
        self.lines = None
        self.used = False          # ¿ya hubo algún mensaje en esta sesión?
        self.resets_at = None      # último "resetsAt" visto en los avisos de límite de uso
        self.lock = threading.Lock()

    def _cmd(self):
        return [CFG["claude_bin"], "-p",
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                "--append-system-prompt", CFG["claude_system"],
                "--tools", "Read", "Grep", "Glob", "Bash", "Write", "Edit",
                # la memoria se escribe sin preguntar; todo comando de Bash pregunta siempre
                # (incluso los de solo lectura) y cualquier otra acción que necesite permiso
                # llega a Fuji como petición, que el usuario acepta o deniega
                "--allowedTools", f"Write({CFG['claude_memory']}/**)", f"Edit({CFG['claude_memory']}/**)",
                "--settings", json.dumps({"permissions": {"ask": ["Bash"]}}),
                "--permission-mode", "manual",
                "--permission-prompt-tool", "stdio",
                "--strict-mcp-config",
                "--effort", CFG["claude_effort"]]

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        try:
            self.proc = subprocess.Popen(self._cmd(), cwd=CFG["claude_cwd"], text=True, bufsize=1,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=open(LOG, "a"))
        except FileNotFoundError:
            self.proc = None
            raise RuntimeError("No encuentro el comando claude (ajusta CLAUDE_BIN)") from None
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc, self.lines), daemon=True).start()

    @staticmethod
    def _pump(proc, q):
        for line in proc.stdout:
            q.put(line)
        q.put(None)  # el proceso terminó

    def warm(self):
        """Arranca el proceso en segundo plano para que el primer mensaje no pague el arranque."""
        def go():
            with self.lock:
                if not self._alive():
                    try:
                        self._start()
                    except Exception as e:
                        log(repr(e))
        threading.Thread(target=go, daemon=True).start()

    def reset(self):
        """Conversación nueva: cierra la sesión actual y deja otra preparada."""
        with self.lock:
            if self._alive():
                self.proc.kill()
            self.proc = None
            self.used = False
        self.warm()

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _control(self, r):
        """Peticiones del CLI: can_use_tool → bocadillo con [S]/[N]."""
        req = r.get("request", {})
        rid = r.get("request_id")
        if req.get("subtype") == "can_use_tool":
            # comandos de confianza (FUJI_TRUSTED_COMMANDS) pasan sin preguntar
            cmd = (req.get("input") or {}).get("command", "").strip()
            trusted = req.get("tool_name") == "Bash" and cmd in TRUSTED_COMMANDS
            allow = trusted or ask_permission(describe_tool(req.get("tool_name"), req.get("input")))
            resp = ({"behavior": "allow", "updatedInput": req.get("input", {})} if allow else
                    {"behavior": "deny", "message": "El usuario ha denegado esta acción."})
            if not allow:
                stop_thinking()
            self._send({"type": "control_response",
                        "response": {"subtype": "success", "request_id": rid, "response": resp}})
        else:
            self._send({"type": "control_response",
                        "response": {"subtype": "error", "request_id": rid,
                                     "error": f"Fuji no soporta {req.get('subtype')}"}})

    def ask(self, text, on_delta=None, images=None):
        with self.lock:
            reply = clean_leak(self._turn(text, on_delta, images)).strip()
            if not reply:
                # el modelo devolvió solo una etiqueta interna: pedirle que conteste de nuevo
                log(f"Respuesta vacía o filtrada para {text[:60]!r}; reintentando")
                reply = clean_leak(self._turn("Tu respuesta anterior llegó vacía. "
                                              "Responde de nuevo a mi último mensaje.", on_delta)).strip()
            return reply

    def _turn(self, text, on_delta=None, images=None):
            if not self._alive():
                self._start()
            self.used = True
            content = text
            if images:
                content = [{"type": "image", "source": {"type": "base64", "media_type": im["media_type"],
                                                        "data": im["data"]}} for im in images]
                content.append({"type": "text", "text": text or "Mira esta imagen."})
            self.proc.stdin.write(json.dumps({"type": "user",
                                              "message": {"role": "user", "content": content}}) + "\n")
            self.proc.stdin.flush()
            acc, new_msg = "", False
            while True:
                try:
                    line = self.lines.get(timeout=180)
                except queue.Empty:
                    self.proc.kill()
                    raise RuntimeError("Claude Code tardó demasiado en responder") from None
                if line is None:
                    raise RuntimeError("Claude Code se cerró inesperadamente (mira ~/.cache/carabot.log)")
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("type") == "control_request":
                    self._control(r)
                    continue
                if r.get("type") == "rate_limit_event":
                    info = r.get("rate_limit_info", {})
                    self.resets_at = info.get("resetsAt") or self.resets_at
                    if info.get("status") == "rejected":
                        go_to_sleep(self.resets_at)
                    continue
                if r.get("type") == "assistant":
                    for b in r.get("message", {}).get("content", []):
                        if b.get("type") == "tool_use":
                            think(describe_tool(b.get("name"), b.get("input")))
                            if b.get("name") != "Bash":  # Bash se apunta al decidir el permiso
                                tlog("tool", describe_tool(b.get("name"), b.get("input")))
                    continue
                if r.get("type") == "user":
                    stop_thinking()  # llegó el resultado de la herramienta
                    continue
                if r.get("type") == "stream_event":
                    ev = r.get("event", {})
                    if ev.get("type") == "message_start":
                        new_msg = bool(acc)  # tras usar una herramienta llega otro mensaje
                    elif ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                        if new_msg:
                            acc, new_msg = acc + " ", False
                        acc += ev["delta"]["text"]
                        shown = clean_leak(acc)
                        if on_delta and shown:
                            on_delta(shown)
                elif r.get("type") == "result":
                    stop_thinking()
                    if r.get("is_error"):
                        msg = str(r.get("result", "error"))
                        if S.sleep_until or "limit" in msg.lower():
                            go_to_sleep(S.sleep_until or self.resets_at)
                            raise Sleeping()
                        raise RuntimeError(f"Claude Code: {msg[:200]}")
                    return (acc or str(r.get("result", ""))).strip()

CLAUDE = ClaudeSession()

def ask_claude(history, on_delta=None, images=None):
    if len(history) == 1 and CLAUDE.used:  # conversación nueva tras "olvida todo" o tecla R
        CLAUDE.reset()
    return CLAUDE.ask(history[-1]["content"], on_delta, images)

def ask_llm(history, on_delta=None, images=None):
    if CFG["backend"] == "claude":
        return ask_claude(history, on_delta, images)
    if images:
        raise RuntimeError(f"El backend {CFG['backend']} no admite imágenes; usa CARA_BACKEND=claude")
    if CFG["backend"] == "groq":
        key = CFG["groq_api_key"]
        if not key:
            raise RuntimeError("Falta la API key de Groq: ponla en CFG['groq_api_key'] o en GROQ_API_KEY")
        r = http_json("https://api.groq.com/openai/v1/chat/completions",
                      {"model": CFG["groq_model"], "max_tokens": 1024, "temperature": 0.7,
                       "reasoning_effort": "low",
                       "messages": [{"role": "system", "content": CFG["system"]}] + history},
                      {"authorization": f"Bearer {key}"})
        return r["choices"][0]["message"]["content"].strip()
    if CFG["backend"] == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("Falta ANTHROPIC_API_KEY")
        r = http_json("https://api.anthropic.com/v1/messages",
                      {"model": CFG["anthropic_model"], "max_tokens": 400,
                       "system": CFG["system"], "messages": history},
                      {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in r["content"] if b.get("type") == "text").strip()
    r = http_json(CFG["ollama_url"],
                  {"model": CFG["ollama_model"], "stream": False,
                   "messages": [{"role": "system", "content": CFG["system"]}] + history}, {})
    return r["message"]["content"].strip()

# ───────────────────────── TTS con lip-sync ─────────────────────────
def synth(text, path):
    if CFG["tts"] == "piper" and CFG["piper_model"]:
        subprocess.run([CFG["piper_bin"], "-m", CFG["piper_model"], "-f", path],
                       input=text.encode(), check=True, capture_output=True)
    else:
        subprocess.run(["espeak-ng", "-v", CFG["espeak_voice"], "-s", "165", "-w", path, text],
                       check=True, capture_output=True)

def speak(text):
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        synth(text, path)
        with wave.open(path) as wf:
            sr, ch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        if sw != 2:
            raise RuntimeError("El TTS debe generar WAV de 16 bits")
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            a = a.reshape(-1, ch).mean(axis=1)
        ref = max(float(np.sqrt(np.mean(a ** 2))) * 1.6, 1e-4)
        pos = [0]
        done = threading.Event()

        def cb(out, frames, t, status):
            if S.interrupt.is_set():
                out.fill(0)
                raise sd.CallbackStop
            chunk = a[pos[0]:pos[0] + frames]
            n = len(chunk)
            out[:n, 0] = chunk
            out[n:, 0] = 0
            S.level = min(1.0, float(np.sqrt(np.mean(chunk ** 2))) / ref) if n else 0.0
            pos[0] += frames
            if n < frames:
                raise sd.CallbackStop

        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32", callback=cb,
                             blocksize=int(sr * 0.03), finished_callback=done.set):
            done.wait()
    finally:
        S.level = 0.0
        os.unlink(path)

# ───────────────────────── comandos de voz locales ─────────────────────────
def norm(t):
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return " ".join("".join(c if c.isalnum() else " " for c in t).split())

def forget():
    S.history.clear()
    if S.transcript:
        tlog("system", "— conversación nueva —")
    if CFG["backend"] == "claude":
        CLAUDE.reset()  # la sesión nueva se prepara ya, en segundo plano

def cmd_forget(_):
    forget()
    return "Vale, empiezo de cero."

def cmd_quit(_):
    S.running_after_speech = False
    return "Hasta luego."

def launch(cmd, answer):
    def run(_):
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return answer
    return run

COMMANDS = [
    (("que hora es",), lambda _: time.strftime("Son las %H y %M."), False),
    (("que dia es hoy",), lambda _: time.strftime("Hoy es %d del %m de %Y."), False),
    (("olvida todo", "nueva conversacion", "borra la memoria"), cmd_forget, False),
    (("apagate", "salir", "adios"), cmd_quit, True),
    (("abre el navegador",), launch(["xdg-open", "https://duckduckgo.com"], "Abriendo el navegador."), False),
]

def local_command(text):
    t = norm(text)
    for phrases, fn, exact in COMMANDS:
        for p in phrases:
            if (t == p) if exact else (p in t):
                return fn(t)
    return None

# ───────────────────────── modo texto ─────────────────────────
VOWELS = set("aeiouáéíóúüAEIOUÁÉÍÓÚÜ")

def ask_and_remember(text, on_delta=None, images=None):
    S.history.append({"role": "user", "content": text})
    try:
        reply = ask_llm(S.history, on_delta, images)
    except Exception:
        S.history.pop()  # no dejar la pregunta huérfana en el historial
        raise
    S.history.append({"role": "assistant", "content": reply})
    del S.history[:-20]
    return reply

def typewrite(reply=None, stream=None):
    """Muestra la respuesta letra a letra moviendo la boca según la letra.
    Con stream={"text","done"} va escribiendo mientras la respuesta aún se está generando."""
    S.interrupt.clear()
    current = (lambda: reply) if stream is None else (lambda: stream["text"])
    finished = (lambda: True) if stream is None else (lambda: stream["done"])
    S.reply, S.shown, S.mode = current(), 0, "speaking"
    delay = 1.0 / max(CFG["type_cps"], 1)
    i = 0
    while not S.interrupt.is_set():
        txt = current()
        if i >= len(txt):
            if finished():
                break
            S.level = 0.0
            time.sleep(0.03)  # esperando más texto
            continue
        ch = txt[i]
        S.reply = txt
        i += 1
        S.shown = i
        if ch in VOWELS:
            S.level = random.uniform(0.6, 1.0)
        elif ch.isalnum():
            S.level = random.uniform(0.2, 0.45)
        else:
            S.level = 0.0
        pause = delay * (6 if ch in ".!?" else 3 if ch in ",;:" else 1)
        time.sleep(pause)
    S.level, S.shown = 0.0, None

def text_pipeline(text, images=None):
    reply = None
    n = len(images or [])
    tlog("user", (text + "  " if text else "") + (f"[{n} imagen{'es' if n > 1 else ''}]" if n else ""))
    try:
        S.heard, S.reply, S.shown = text, "", None
        reply = None if images else local_command(text)
        if reply is None and sleeping():
            tlog("system", "Estaba durmiendo: no envié el mensaje.")
            return
        if reply is None and CFG["backend"] == "claude":
            # escribir según llega la respuesta, sin esperar a que termine
            S.mode = "thinking"
            stream = {"text": "", "done": False}
            typer = threading.Thread(target=typewrite, kwargs={"stream": stream}, daemon=True)

            def on_delta(t):
                stream["text"] = t
                if not typer.is_alive() and not typer.ident:
                    typer.start()
            try:
                reply = ask_and_remember(text, on_delta, images)
                stream["text"] = reply
            finally:
                stream["done"] = True
            if reply:
                tlog("assistant", reply)
            if typer.ident:
                typer.join()
            elif reply:
                typewrite(reply)
            return
        if reply is None:
            S.mode = "thinking"
            reply = ask_and_remember(text)
        if reply:
            tlog("assistant", reply)
            typewrite(reply)
    except Sleeping:
        S.reply, S.shown = "", None
        tlog("system", "Me quedé dormido: límite de uso de Claude.")
    except Exception as e:
        S.reply = f"Error: {e}"
        tlog("system", f"Error: {e}")
        log(repr(e))
    finally:
        S.mode = "idle"
        if not S.running_after_speech:
            time.sleep(0.8)
            S.running = False

def open_input():
    if S.mode == "idle":
        S.typing, S.buf = True, ""

def submit_input():
    text, images = S.buf.strip(), S.attachments
    S.typing, S.buf, S.attachments, S.input_note = False, "", [], ""
    if text or images:
        S.mode = "thinking"
        threading.Thread(target=text_pipeline, args=(text, images), daemon=True).start()

def handle_typing_key(k):
    if k in ("\n", "\r", curses.KEY_ENTER):
        submit_input()
    elif k == "\x1b":
        S.typing, S.buf, S.attachments, S.input_note = False, "", [], ""
    elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        if S.buf:
            S.buf = S.buf[:-1]
        elif S.attachments:  # con el texto vacío, Retroceso quita la última imagen
            S.attachments.pop()
    elif k == "\x15":  # Ctrl+U borra la línea
        S.buf = ""
    elif k == "\x16":  # Ctrl+V pega del portapapeles (texto o imagen)
        paste_clipboard()
    elif isinstance(k, str) and k.isprintable() and len(S.buf) < MAX_BUF:
        S.buf += k
        S.input_note = ""

# ───────────────────────── pipeline de voz ─────────────────────────
def pipeline(audio):
    S.running_after_speech = True
    try:
        if len(audio) < CFG["rate"] * 0.3:
            S.reply = "Demasiado corto. Mantén pulsado un poco más."
            return
        S.mode = "transcribing"
        text = transcribe(audio)
        if not text:
            S.reply = "No te he oído."
            return
        S.heard, S.reply = text, ""
        tlog("user", text)
        reply = local_command(text)
        if reply is None and sleeping():
            tlog("system", "Estaba durmiendo: no envié el mensaje.")
            return
        if reply is None:
            S.mode = "thinking"
            reply = ask_and_remember(text)
        S.reply, S.shown = reply, None
        if reply:
            tlog("assistant", reply)
            S.interrupt.clear()
            S.mode = "speaking"
            speak(reply)
    except Sleeping:
        S.reply, S.shown = "", None
        tlog("system", "Me quedé dormido: límite de uso de Claude.")
    except Exception as e:
        S.reply = f"Error: {e}"
        tlog("system", f"Error: {e}")
        log(repr(e))
    finally:
        S.mode = "idle"
        if not S.running_after_speech:
            S.running = False

def on_space():
    if S.mode == "idle":
        if sd is None:
            S.reply = "Falta sounddevice/PortAudio: no hay micrófono."
            return
        try:
            REC.start()
            S.heard, S.reply = "", ""
            S.mode = "listening"
        except Exception as e:
            S.reply = f"Micrófono: {e}"
    elif S.mode == "listening":
        audio = REC.stop()
        S.mode = "transcribing"
        threading.Thread(target=pipeline, args=(audio,), daemon=True).start()
    elif S.mode == "speaking":
        S.interrupt.set()

# ───────────────────────── interfaz ─────────────────────────
MODE_TXT = {"idle": "en espera", "listening": "escuchando…", "transcribing": "transcribiendo…",
            "thinking": "pensando…", "speaking": "hablando", "asking": "pidiendo permiso",
            "sleeping": "durmiendo"}

BUBBLE_ROWS = 9  # filas reservadas sobre la cabeza

def bubble_lines(text, width, asking):
    """Bocadillo de pensamiento: nube con el texto y una estela de burbujas hacia la cabeza."""
    inner = max(10, width - 4)
    rows = textwrap.wrap(text, inner, break_long_words=True, break_on_hyphens=False)[:4] or [""]
    if asking:
        rows += ["", "¿Lo hago?   [S] sí    [N] no"]
    iw = max(len(r) for r in rows)
    out = [" .-" + "~" * iw + "-.", ]
    out += ["(  " + r.ljust(iw) + "  )" for r in rows]
    out += [" '-" + "~" * iw + "-'"]
    return out

def zzz_lines(now):
    """Zetas que suben flotando mientras duerme."""
    f = int(now * 1.5) % 3
    frames = [["      Z", "    z", "  z"], ["    Z", "  z", "      z"], ["  Z", "      z", "    z"]]
    return frames[f]

def reveal(text, shown, width, max_lines=8):
    """Ajusta el texto completo y muestra solo las primeras `shown` letras,
    así las palabras no saltan de línea mientras aparecen."""
    wrapped = textwrap.wrap(text, width) or [""]
    if shown is None:
        return wrapped[-max_lines:]
    out, left = [], shown
    for line in wrapped:
        if left <= 0:
            break
        out.append(line[:left])
        left -= len(line) + 1  # +1 por el espacio que wrap elimina
    return out[-max_lines:]

def draw_input(scr, y, w, width):
    x = (w - width) // 2 - 2
    frame = curses.color_pair(0) | curses.A_BOLD
    title = " Escribe tu pregunta "
    if S.attachments:
        title += "· " + " ".join(f"[img {i + 1}: {a['size'] / 1e6:.1f} MB]"
                                 for i, a in enumerate(S.attachments)) + " "
    if len(S.buf) > 200:
        title += f"· {len(S.buf)} caracteres "
    title = title[:width]
    put(scr, y, x, "╭" + title + "─" * (width + 2 - len(title)) + "╮", frame)
    shown = S.buf.replace("\n", " ⏎ ")  # saltos de línea del texto pegado
    rows = textwrap.wrap(shown + "▌", width, drop_whitespace=False) or ["▌"]
    rows = rows[-3:]
    rows += [""] * (3 - len(rows))
    for i, r in enumerate(rows):
        put(scr, y + 1 + i, x, "│ ", frame)
        put(scr, y + 1 + i, x + 2, r.ljust(width), curses.color_pair(2) | curses.A_BOLD)
        put(scr, y + 1 + i, x + 2 + width, " │", frame)
    note = f" {S.input_note} " if S.input_note else ""
    put(scr, y + 4, x, "╰" + note + "─" * max(0, width + 2 - len(note)) + "╯",
        curses.color_pair(4) | curses.A_BOLD if note else frame)

HIST_STYLE = {  # rol → (etiqueta, color, negrita)
    "user": ("Tú", 3, True), "assistant": ("Fuji", 2, True),
    "tool": ("", 4, False), "system": ("", 1, False),
}

def history_lines(width):
    """Convierte la transcripción en líneas (texto, atributo) ya ajustadas al ancho."""
    out = []
    entries = list(S.transcript)
    # respuesta que se está generando ahora mismo
    if S.mode in ("thinking", "speaking") and entries and entries[-1][0] == "user":
        entries.append(("assistant", (S.reply or "…") + " ▌", time.strftime("%H:%M")))
    for role, text, hhmm in entries:
        label, color, bold = HIST_STYLE.get(role, ("", 2, False))
        attr = curses.color_pair(color) | (curses.A_BOLD if bold else 0)
        if role in ("user", "assistant"):
            out.append((f"{hhmm}  {label}", attr))
            body = curses.color_pair(2) if role == "assistant" else curses.color_pair(3)
            for para in text.split("\n"):
                for l in textwrap.wrap(para, width - 2) or [""]:
                    out.append(("  " + l, body))
            out.append(("", 0))
        else:
            mark = "⚙ " if role == "tool" else "· "
            for i, l in enumerate(textwrap.wrap(text, width - 4) or [""]):
                out.append((("  " + mark if i == 0 else "    ") + l, attr))
            if role == "system":
                out.append(("", 0))
    return out

def draw_history(scr, h, w):
    bw = max(30, min(100, w - 4))
    bh = max(6, h - 2)
    x0, y0 = (w - bw) // 2, 0
    rows = bh - 2
    lines = history_lines(bw - 4)
    max_scroll = max(0, len(lines) - rows)
    S.hist_scroll = max(0, min(S.hist_scroll, max_scroll))
    end = len(lines) - S.hist_scroll
    visible = lines[max(0, end - rows):end]
    frame = curses.color_pair(2) | curses.A_BOLD
    title = f" Historial · {sum(1 for e in S.transcript if e[0] == 'user')} preguntas "
    put(scr, y0, x0, "╭─" + title + "─" * max(0, bw - 3 - len(title)) + "╮", frame)
    for i in range(rows):
        text, attr = visible[i] if i < len(visible) else ("", 0)
        put(scr, y0 + 1 + i, x0, "│ ", frame)
        put(scr, y0 + 1 + i, x0 + 2, text[:bw - 4].ljust(bw - 4), attr)
        put(scr, y0 + 1 + i, x0 + bw - 2, " │", frame)
    more_up = "↑ más arriba" if end - rows > 0 else ""
    more_dn = f"↓ {S.hist_scroll} líneas más abajo" if S.hist_scroll else ""
    foot = " · ".join(t for t in (more_up, more_dn) if t)
    foot = f" {foot} " if foot else ""
    put(scr, y0 + bh - 1, x0, "╰" + foot + "─" * max(0, bw - 2 - len(foot)) + "╯", frame)
    if not S.transcript:
        put(scr, y0 + 2, x0 + 4, "Aún no habéis hablado. Pulsa T para escribirle.", curses.color_pair(1))
    return rows

def draw(scr, anim, now):
    h, w = scr.getmaxyx()
    if S.show_history:
        S.hist_rows = draw_history(scr, h, w)
        bar = " [↑↓/rueda] desplazar  [RePág/AvPág] página  [Inicio/Fin] extremos  [H/Esc] cerrar"
        put(scr, h - 1, 0, bar.ljust(w), curses.A_REVERSE)
        return
    asleep = sleeping()
    mode = "sleeping" if asleep else "asking" if S.perm else S.mode
    trick = anim.update_trick(now, "typing" if S.typing or S.thought and now < S.thought_until else mode)
    face = build_face(anim.eye(now, mode), anim.mouth(mode, S.level))
    fh, fw = len(face), len(face[0])
    top = max(BUBBLE_ROWS + 1, (h - (fh + 9 + BUBBLE_ROWS)) // 2 + BUBBLE_ROWS)
    left = max(0, (w - fw) // 2)

    # ── encima de la cabeza: bocadillo de pensamiento o zetas ──
    bub_w = max(24, min(70, w - 4))
    bx0 = max(0, (w - bub_w) // 2 - 3)
    for yy in range(max(0, top - BUBBLE_ROWS), top):
        put(scr, yy, bx0, " " * (bub_w + 6))
    if trick == "ball":
        dx, hgt = anim.ball_pos(now)
        put(scr, top - 1 - hgt, left + fw // 2 + dx, "●", curses.color_pair(4) | curses.A_BOLD)
    if asleep or trick == "nap":
        for i, z in enumerate(zzz_lines(now)):
            put(scr, top - 4 + i, left + fw - 4, z, curses.color_pair(3) | curses.A_BOLD)
    elif S.thought and now < S.thought_until:
        cloud = bubble_lines(S.thought, bub_w, bool(S.perm))
        trail = ["  O", "o"] if len(cloud) + 2 <= BUBBLE_ROWS else []  # burbujas menguando hacia la cabeza
        block = cloud + trail
        y = top - len(block)
        cw = len(cloud[0])
        cx = max(0, (w - cw) // 2)
        battr = curses.color_pair(4 if S.perm else 3) | curses.A_BOLD
        for i, line in enumerate(cloud):
            put(scr, y + i, cx, line, battr)
        for i, t in enumerate(trail):
            put(scr, y + len(cloud) + i, left + fw // 2 - 4, t, battr)

    color = {"idle": 2, "listening": 4, "speaking": 2}.get(mode, 3)
    attr = curses.color_pair(color) | curses.A_BOLD
    for y in range(top, top + fh + 1):  # la fila de encima es del bocadillo
        put(scr, y, left - 2, " " * (fw + 4))
    for i, line in enumerate(face):
        put(scr, top + i, left, line, attr)

    sub_w = max(20, min(64, w - 6))
    y0 = top + fh + 2
    for yy in range(y0, y0 + 11):  # limpia caja y respuesta anteriores
        put(scr, yy, (w - sub_w) // 2 - 2, " " * (sub_w + 4))
    if asleep:
        until = time.strftime("%H:%M", time.localtime(S.sleep_until)) if S.sleep_until else "?"
        msg = [("Zzz… me he quedado sin energía (límite de uso de Claude).", curses.color_pair(3)),
               (f"Vuelvo en {fmt_left(S.sleep_until - now)}  ·  a las {until}",
                curses.color_pair(2) | curses.A_BOLD)]
        for i, (l, a) in enumerate(msg):
            put(scr, y0 + i, (w - sub_w) // 2 - 1, " " + l.center(sub_w) + " ", a)
    elif S.typing:
        draw_input(scr, y0, w, sub_w)
    else:
        lines = []
        if S.heard:
            lines += [(l, curses.color_pair(3)) for l in textwrap.wrap("> " + S.heard, sub_w)[:2]]
        if S.reply:
            lines += [(l, curses.color_pair(2) | curses.A_BOLD) for l in reveal(S.reply, S.shown, sub_w)]
        for i, (l, a) in enumerate(lines):
            put(scr, y0 + i, (w - sub_w) // 2 - 1, " " + l.ljust(sub_w) + " ", a)

    if S.perm:
        bar = " [S] permitir  [N/Esc] denegar   ·  pidiendo permiso"
    elif S.typing:
        bar = " [Enter] enviar  [Ctrl+V] pegar texto/imagen  [Esc] cancelar  [Ctrl+U] borrar"
    else:
        bar = (f" [T/Enter] escribir  [ESPACIO] voz/saltar  [H] historial  [r] olvidar  [q] salir"
               f"   ·  {MODE_TXT[mode]}  ·  {CFG['backend']}")
    if not WHISPER_READY.is_set():
        bar += "  ·  cargando Whisper…"
    put(scr, h - 1, 0, bar.ljust(w), curses.A_REVERSE)

def handle_history_key(k):
    page = max(1, getattr(S, "hist_rows", 10) - 2)
    if k in ("h", "H", "\x1b"):
        S.show_history, S.need_clear = False, True
    elif k in (curses.KEY_UP, "k"):
        S.hist_scroll += 1
    elif k in (curses.KEY_DOWN, "j"):
        S.hist_scroll -= 1
    elif k == curses.KEY_PPAGE:
        S.hist_scroll += page
    elif k == curses.KEY_NPAGE:
        S.hist_scroll -= page
    elif k == curses.KEY_HOME:
        S.hist_scroll = 10 ** 9   # se recorta al máximo al dibujar
    elif k == curses.KEY_END:
        S.hist_scroll = 0
    elif k == curses.KEY_MOUSE:
        try:
            _, _, _, _, bstate = curses.getmouse()
        except curses.error:
            return
        if bstate & curses.BUTTON4_PRESSED:
            S.hist_scroll += 3
        elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
            S.hist_scroll -= 3
    elif k in ("t", "T", "\n", "\r", curses.KEY_ENTER):
        S.show_history, S.need_clear = False, True
        open_input()
    S.hist_scroll = max(0, S.hist_scroll)

def main(scr):
    curses.curs_set(0)
    scr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_WHITE, -1)
    curses.init_pair(3, curses.COLOR_CYAN, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    # solo la rueda del ratón (deja libre la selección de texto del terminal)
    curses.mousemask(curses.BUTTON4_PRESSED | getattr(curses, "BUTTON5_PRESSED", 0))
    scr.erase()
    h, w = scr.getmaxyx()
    rain, anim = Rain(h, w), Anim()

    while S.running:
        try:
            k = scr.get_wch()  # admite ñ, tildes, etc.
        except curses.error:
            k = -1
        if k == "\x1b":
            pasted = read_bracketed_paste(scr)
            if pasted is not None:
                if not S.typing and S.mode == "idle" and not S.perm and not S.show_history:
                    open_input()
                if S.typing:
                    insert_text(pasted)
                k = -1
        if S.perm and k in ("s", "S", "y", "Y"):
            answer_permission(True)
        elif S.perm and k in ("n", "N", "\x1b"):
            answer_permission(False)
        elif S.perm and k not in (-1, curses.KEY_RESIZE, "q", "Q"):
            pass  # mientras pide permiso, solo S / N / q
        elif S.typing and k != -1 and k != curses.KEY_RESIZE:
            handle_typing_key(k)
        elif S.show_history and k not in (-1, curses.KEY_RESIZE, "q", "Q"):
            handle_history_key(k)
        elif k in ("h", "H"):
            S.show_history, S.hist_scroll, S.need_clear = True, 0, True
        elif k in ("t", "T", "\n", "\r", curses.KEY_ENTER):
            open_input()
        elif k == " ":
            on_space()
        elif k in ("q", "Q"):
            S.interrupt.set()
            break
        elif k in ("r", "R"):
            forget()
            S.heard, S.reply, S.shown = "", "                Conversación olvidada.", None
        if S.need_clear:
            S.need_clear = False
            scr.erase()
        nh, nw = scr.getmaxyx()
        if k == curses.KEY_RESIZE or (nh, nw) != (h, w):
            h, w = nh, nw
            scr.erase()
            rain = Rain(h, w)
        # rain.step(scr)
        draw(scr, anim, time.time())
        scr.refresh()
        time.sleep(1 / 30)

if __name__ == "__main__":
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")  # Esc responde al instante
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    _lf = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(_lf, 2)
    threading.Thread(target=load_whisper, daemon=True).start()
    load_sleep()
    if CFG["backend"] == "claude" and not S.sleep_until:
        CLAUDE.warm()
    sys.stdout.write("\x1b]0;Fuji\x07")  # título de la ventana
    sys.stdout.flush()
    sys.stdout.write("\x1b[?2004h")  # pegado entre corchetes: el terminal marca lo pegado
    sys.stdout.flush()
    try:
        curses.wrapper(main)
    finally:
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()

