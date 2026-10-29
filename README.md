# Fuji

Una cara pixel art que vive en tu terminal, sobre una lluvia estilo Matrix, y habla con tu **Claude Code**.
Parpadea, mueve la boca al hablar y puedes escribirle o hablarle por voz.

## Requisitos

- Linux con Python 3.9+
- [Claude Code](https://claude.com/claude-code) instalado y con sesión iniciada (`claude`)
- Opcional: `espeak-ng` (voz hablada), `wl-clipboard` (pegar), `portaudio` (micrófono)

## Instalación

```sh
git clone https://github.com/marcosmedinabri/Fuji.git
cd Fuji
./install.sh
fuji
```

El instalador pregunta antes de instalar nada con `sudo`. Fuji se instala en `~/.local/share/fuji` con su propio entorno de Python, y el comando `fuji` queda en `~/.local/bin`.

## Uso

| Tecla | Acción |
|---|---|
| `T` / `Enter` | escribir una pregunta |
| `Espacio` | hablar por voz / saltar la animación |
| `H` | historial |
| `r` | olvidar la conversación |
| `q` | salir |

Fuji usa tu Claude Code: puede leer archivos y proponer comandos, pero **cada comando de Bash te lo pregunta** antes de ejecutarlo. Solo escribe sin preguntar en la carpeta de memoria de Claude Code.

## Configuración

Todo se ajusta en `~/.config/fujibot/env` (ver [`env.example`](env.example)). Ese archivo es privado: ahí van tus claves si usas otro backend, y nunca debe subirse a ningún repositorio.

## Desinstalar

```sh
./uninstall.sh
```
