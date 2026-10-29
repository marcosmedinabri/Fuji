```
┌────────────────────────────┐
│                            │
│                            │
│       ██      ██    ██     │
│       ██      ██    ██     │     {________
│               ██           │     {__                 {__ {_
│                            │     {__      {__  {__
│           ██████           │     {______  {__  {__   {__{__
│                            │     {__      {__  {__   {__{__
│                            │     {__      {__  {__   {__{__
│                            │     {__        {__{__   {__{__
│                            │                      {___
├────────────────────────────┤
│                            │        Tu asistente personal en la terminal,
│     ━━━           ━━━  │            conectado a Claude Code
│                            │
└────────────────────────────┘
   ┌─────────────────────┐
   └─────────────────────┘
```

> *”Parpadea, escucha y responde.”*

**Fuji** es una cara pixel art que vive en tu terminal, sobre una lluvia de caracteres estilo Matrix, y usa tu **Claude Code** como cerebro. Parpadea, mueve la boca sincronizada con la voz y puedes escribirle o hablarle: te contesta por escrito, letra a letra, y en voz alta.

-----

## ¿Cómo funciona?

```
micrófono ─► faster-whisper (voz a texto, local) ─┐
teclado ──────────────────────────────────────────┴─► Claude Code ─► espeak-ng (texto a voz) + boca sincronizada
```

Fuji mantiene abierta una sesión de `claude` en segundo plano, así que la conversación continúa entre mensajes y comparte la memoria de tu Claude Code.

|Puede…                       |Permiso                         |
|-----------------------------|--------------------------------|
|Leer archivos                |Libre                           |
|Escribir en su memoria       |Libre                           |
|Ejecutar comandos de Bash    |**Te pregunta siempre** `[S]/[N]`|
|Escribir fuera de su memoria |**Te pregunta siempre**         |

-----

## Requisitos

- Linux con **Python 3.9+**
- [Claude Code](https://claude.com/claude-code) instalado y con sesión iniciada (`claude`)
- Opcional: `espeak-ng` (voz hablada), `wl-clipboard` (pegar imágenes y texto), `portaudio` (micrófono)

-----

## Instalación

```sh
git clone https://github.com/marcosmedinabri/Fuji.git
cd Fuji
./install.sh
fuji
```

El instalador:

1. Comprueba Python y Claude Code.
2. Pregunta si quieres **voz** (micrófono + voz hablada, ~500 MB).
3. Pregunta antes de instalar nada con `sudo` (pacman, apt o dnf).
4. Crea su propio entorno de Python en `~/.local/share/fuji`, sin tocar el del sistema.
5. Deja el comando `fuji` en `~/.local/bin` y tu configuración privada en `~/.config/fujibot/env`.

-----

## Controles

|Tecla        |Acción                                   |
|-------------|-----------------------------------------|
|`T` / `Enter`|Escribir una pregunta                    |
|`Espacio`    |Hablar por voz · saltar la animación     |
|`H`          |Historial                                |
|`r`          |Olvidar la conversación                  |
|`q`          |Salir                                    |

-----

## Configuración

Todo se ajusta en `~/.config/fujibot/env` (plantilla en [`env.example`](env.example)):

|Variable               |Por defecto|Para qué                                         |
|-----------------------|-----------|-------------------------------------------------|
|`CARA_BACKEND`         |`claude`   |`claude` · `anthropic` · `groq` · `ollama`       |
|`CLAUDE_CWD`           |`~`        |Carpeta de trabajo (y memoria) de Claude Code    |
|`CLAUDE_EFFORT`        |`low`      |`low` responde más rápido                        |
|`FUJI_TRUSTED_COMMANDS`|—          |Comandos que puede ejecutar sin preguntarte      |
|`WHISPER_MODEL`        |`small`    |`tiny` · `base` · `small` · `medium`             |

> Ese archivo es **privado**: ahí van tus API keys si usas otro backend. Nunca lo subas a ningún repositorio.

-----

## Desinstalar

```sh
./uninstall.sh
```
