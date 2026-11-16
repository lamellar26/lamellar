#!/bin/sh
# lamellar one-line installer for Linux and macOS.
#
#   curl -fsSL https://lamellar.org/install.sh | sh
#
# What it does: checks Python 3.10+, installs Ollama if missing, picks a model that fits your GPU,
# downloads it and builds a Modelfile with the right context length, writes the config, starts the
# web entry, opens your browser, and finally runs "lamellar doctor".
#
# Options (environment variables), all optional:
#   LAMELLAR_HOME   where config, logs and the code live      (default: ~/.lamellar)
#   LAMELLAR_UI     builtin | openwebui | both                (default: ask; builtin if no terminal)
#   LAMELLAR_MODEL  force a model, e.g. qwen3.5:4b            (default: picked from VRAM)
#   LAMELLAR_CTX    force the context length in tokens        (default: picked with the model)
#   LAMELLAR_PORT   web entry port                            (default: 8102)
#   LAMELLAR_SRC    code source: a .tar.gz URL or a local directory
#   LAMELLAR_YES=1  never ask, take the defaults
#   LAMELLAR_NO_BROWSER=1  do not open a browser at the end
set -eu

HOME_DIR="${LAMELLAR_HOME:-$HOME/.lamellar}"
SRC="${LAMELLAR_SRC:-https://github.com/lamellar26/lamellar/archive/refs/heads/main.tar.gz}"
PORT="${LAMELLAR_PORT:-8102}"
BIN_DIR="$HOME/.local/bin"
OS="$(uname -s)"
OLLAMA_HOSTPORT="${OLLAMA_HOST:-127.0.0.1:11434}"; OLLAMA_HOSTPORT="${OLLAMA_HOSTPORT#http://}"
export OLLAMA_HOST="$OLLAMA_HOSTPORT"   # ollama serve/pull/create all read this
OLLAMA_URL="http://$OLLAMA_HOSTPORT"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Questions go through /dev/tty, because with "curl | sh" stdin is the script itself.
ask() {  # ask "question" default -> echoes the answer
  if [ "${LAMELLAR_YES:-}" = 1 ] || ! ( : </dev/tty ) 2>/dev/null; then echo "$2"; return; fi
  printf '%s [%s]: ' "$1" "$2" >/dev/tty
  read -r a </dev/tty || a=""
  echo "${a:-$2}"
}

SUDO=""
need_root() {  # sets SUDO, or returns 1 if we cannot get root
  if [ "$(id -u)" = 0 ]; then SUDO=""; return 0; fi
  if have sudo; then SUDO="sudo"; return 0; fi
  return 1
}

fetch() {  # fetch URL FILE
  if have curl; then curl -fsSL "$1" -o "$2"; elif have wget; then wget -qO "$2" "$1"; else die "need curl or wget"; fi
}

# Fresh Linux images often lack curl, CA certificates and zstd (the Ollama installer needs all three).
if [ "$OS" = Linux ] && { ! have curl || { ! have ollama && ! have zstd; }; } && need_root; then
  say "Installing curl and zstd"
  if have apt-get; then $SUDO apt-get update -qq && $SUDO apt-get install -y -qq curl ca-certificates zstd >/dev/null
  elif have dnf; then $SUDO dnf install -y -q curl zstd
  elif have pacman; then $SUDO pacman -Sy --noconfirm curl zstd
  fi
fi

# ---------- 1. Python ----------
find_python() {
  for p in python3.13 python3.12 python3.11 python3.10 python3; do
    if have "$p" && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then echo "$p"; return 0; fi
  done
  return 1
}

say "Checking Python 3.10+"
if ! PY="$(find_python)"; then
  if [ "$OS" = Linux ] && need_root; then
    if have apt-get; then $SUDO apt-get update -qq && $SUDO apt-get install -y -qq python3 >/dev/null
    elif have dnf; then $SUDO dnf install -y -q python3
    elif have pacman; then $SUDO pacman -Sy --noconfirm python
    fi
  elif [ "$OS" = Darwin ] && have brew; then brew install python@3.12
  fi
  PY="$(find_python)" || die "Python 3.10 or newer is required. Install it (https://www.python.org/downloads/) and run this again."
fi
say "Using $($PY --version 2>&1)"

# ---------- 2. Ollama ----------
ollama_up() { have curl && curl -fsS "$OLLAMA_URL/api/version" >/dev/null 2>&1 || "$PY" - "$OLLAMA_URL" <<'EOF' 2>/dev/null
import sys, urllib.request; urllib.request.urlopen(sys.argv[1] + '/api/version', timeout=3)
EOF
}

WE_INSTALLED_OLLAMA=0
say "Checking Ollama"
if ! have ollama; then
  say "Installing Ollama (official installer)"
  if [ "$OS" = Linux ]; then
    tmp="$(mktemp)"; fetch https://ollama.com/install.sh "$tmp"; sh "$tmp"; rm -f "$tmp"
  elif [ "$OS" = Darwin ]; then
    if have brew; then brew install ollama; else die "Install Ollama from https://ollama.com/download (open the app once), then run this again."; fi
  else
    die "Unsupported system $OS. Windows is not supported yet."
  fi
  have ollama || die "Ollama install failed. Install it from https://ollama.com/download and run this again."
  WE_INSTALLED_OLLAMA=1
fi

# Large contexts only fit on 16 GB cards with a 4-bit KV cache and flash attention.
if [ "$WE_INSTALLED_OLLAMA" = 1 ] && [ "$OS" = Linux ] && have systemctl && [ -d /etc/systemd/system ] && need_root \
   && systemctl list-unit-files ollama.service >/dev/null 2>&1; then
  $SUDO mkdir -p /etc/systemd/system/ollama.service.d
  printf '[Service]\nEnvironment="OLLAMA_KV_CACHE_TYPE=q4_0"\nEnvironment="OLLAMA_FLASH_ATTENTION=1"\n' \
    | $SUDO tee /etc/systemd/system/ollama.service.d/lamellar.conf >/dev/null
  $SUDO systemctl daemon-reload && $SUDO systemctl restart ollama || true
fi

mkdir -p "$HOME_DIR/logs"
if ! ollama_up; then
  say "Starting Ollama in the background (log: $HOME_DIR/logs/ollama.log)"
  OLLAMA_KV_CACHE_TYPE=q4_0 OLLAMA_FLASH_ATTENTION=1 nohup ollama serve >>"$HOME_DIR/logs/ollama.log" 2>&1 &
  echo $! >"$HOME_DIR/ollama.pid"
  i=0; while ! ollama_up; do i=$((i+1)); [ $i -gt 30 ] && die "Ollama did not start. See $HOME_DIR/logs/ollama.log"; sleep 1; done
fi
say "Ollama is running"

# ---------- 3. GPU -> model ----------
VRAM=0; GPU=none
if have nvidia-smi && v="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -n1)" && [ -n "$v" ]; then
  VRAM="${v%%.*}"; GPU=nvidia
elif [ "$OS" = Darwin ] && [ "$(uname -m)" = arm64 ]; then
  VRAM=$(( $(sysctl -n hw.memsize) / 1048576 )); GPU=apple
elif [ "$OS" = Linux ] && have lspci && lspci 2>/dev/null | grep -qi nvidia; then
  warn "NVIDIA card found but the driver is not installed (no nvidia-smi). Using CPU for now."
  warn "To fix: install the NVIDIA driver (Ubuntu: sudo ubuntu-drivers install), reboot, run this installer again."
fi

# Provisional tiers; docs/MODELS.md will replace these with measured results.
if [ "$GPU" = apple ]; then  # unified memory: leave room for the system
  if   [ "$VRAM" -ge 32000 ]; then MODEL=qwen3.8:27b; CTX=98304
  elif [ "$VRAM" -ge 16000 ]; then MODEL=qwen3.5:9b;  CTX=32768
  else                             MODEL=qwen3.5:4b;  CTX=32768; fi
elif [ "$GPU" = nvidia ]; then
  if   [ "$VRAM" -ge 15500 ]; then MODEL=qwen3.8:27b; CTX=98304
  elif [ "$VRAM" -ge 7500 ];  then MODEL=qwen3.5:9b;  CTX=32768
  elif [ "$VRAM" -ge 3500 ];  then MODEL=qwen3.5:4b;  CTX=32768
  else                             MODEL=qwen3.5:2b;  CTX=16384; fi
else
  MODEL=qwen3.5:2b; CTX=16384
  warn "No usable GPU: using the smallest model ($MODEL). It works, but answers will be slow and simpler."
fi
MODEL="${LAMELLAR_MODEL:-$MODEL}"; CTX="${LAMELLAR_CTX:-$CTX}"
say "GPU: $GPU (${VRAM} MB) -> model $MODEL, context $CTX"

# ---------- 4. pull + Modelfile ----------
LOCAL_MODEL="lamellar-$(echo "$MODEL" | tr ':/' '--'):$((CTX / 1024))k"
say "Downloading $MODEL (this can take a while the first time)"
ollama pull "$MODEL"
mf="$(mktemp)"; printf 'FROM %s\nPARAMETER num_ctx %s\n' "$MODEL" "$CTX" >"$mf"
ollama create "$LOCAL_MODEL" -f "$mf" >/dev/null; rm -f "$mf"
say "Built $LOCAL_MODEL"

# ---------- 5. code + config ----------
say "Installing lamellar into $HOME_DIR/app"
stage="$(mktemp -d)"
case "$SRC" in
  http://*|https://*) fetch "$SRC" "$stage/src.tar.gz"; tar -xzf "$stage/src.tar.gz" -C "$stage"; rm -f "$stage/src.tar.gz"
                      new="$(find "$stage" -mindepth 1 -maxdepth 1 -type d | head -n1)" ;;
  *.tar.gz)           tar -xzf "$SRC" -C "$stage"; new="$(find "$stage" -mindepth 1 -maxdepth 1 -type d | head -n1)" ;;
  *)                  [ -f "$SRC/cli.py" ] || die "LAMELLAR_SRC=$SRC is not a lamellar directory"
                      mkdir "$stage/src"; (cd "$SRC" && tar --exclude=.git -cf - .) | (cd "$stage/src" && tar -xf -); new="$stage/src" ;;
esac
[ -f "$new/cli.py" ] || die "download did not contain lamellar (no cli.py)"
[ -d "$HOME_DIR/app" ] && mv "$HOME_DIR/app" "$HOME_DIR/app.prev.$$"
mv "$new" "$HOME_DIR/app"; rm -rf "$stage" "$HOME_DIR/app.prev.$$"

UI="${LAMELLAR_UI:-}"
if [ -z "$UI" ]; then
  if [ "${LAMELLAR_YES:-}" != 1 ] && ( : </dev/tty ) 2>/dev/null; then
    printf 'Which web interface?\n  1) built-in (simplest, http://localhost:%s/)\n  2) Open WebUI (needs Docker)\n  3) both\n' "$PORT" >/dev/tty
  fi
  case "$(ask 'Choose 1, 2 or 3' 1)" in 2) UI=openwebui ;; 3) UI=both ;; *) UI=builtin ;; esac
fi
BIND=127.0.0.1; KEY=""; BUILTIN=true; WEBUI_PORT=3000
if [ "$UI" != builtin ]; then
  if have docker; then
    BIND=0.0.0.0   # the Open WebUI container must reach us; protect with a key
    KEY="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  else
    warn "Docker not found, so Open WebUI cannot be set up automatically. Using the built-in interface."
    UI=builtin
  fi
fi
[ "$UI" = openwebui ] && BUILTIN=false

if [ -f "$HOME_DIR/worker.json" ] && [ -f "$HOME_DIR/entry.json" ]; then
  cp "$HOME_DIR/worker.json" "$HOME_DIR/worker.json.bak"; cp "$HOME_DIR/entry.json" "$HOME_DIR/entry.json.bak"
  say "Existing config backed up to *.json.bak"
fi
"$PY" - "$HOME_DIR" "$LOCAL_MODEL" "$CTX" "$PORT" "$BIND" "$KEY" "$BUILTIN" "$WEBUI_PORT" "$OLLAMA_URL" <<'EOF'
import json, os, sys
home, model, ctx, port, bind, key, builtin, wport, ourl = sys.argv[1:]
w = {'model': model, 'endpoint': ourl + '/v1', 'context': int(ctx), 'max_output_tokens': 8000,
     'think': False, 'packs': [], 'packs_dir': [os.path.join(home, 'packs')], 'armor_db': os.path.join(home, 'armor.db'),
     'memory': True, 'state_dir': home}
e = {'port': int(port), 'bind': bind, 'api_key': key, 'model_name': 'lamellar', 'worker_config': os.path.join(home, 'worker.json'),
     'workspace': os.path.join(home, 'work'), 'webui_url': f'http://127.0.0.1:{wport}', 'state_dir': home, 'ui': builtin == 'true'}
for name, d in (('worker.json', w), ('entry.json', e)):
    p = os.path.join(home, name)
    with open(p, 'w', encoding='utf-8') as f: json.dump(d, f, indent=1); f.write('\n')
    os.chmod(p, 0o600)
EOF
mkdir -p "$HOME_DIR/packs" "$HOME_DIR/work"

# ---------- launcher ----------
mkdir -p "$BIN_DIR"
cat >"$BIN_DIR/lamellar" <<EOF
#!/bin/sh
# lamellar launcher (written by install.sh)
H="$HOME_DIR"; PY="$PY"; PORT="$PORT"; OU="$OLLAMA_URL"; export OLLAMA_HOST="$OLLAMA_HOSTPORT"
EOF
cat >>"$BIN_DIR/lamellar" <<'EOF'
up() { "$PY" -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=2)" "$1" 2>/dev/null; }
ensure_ollama() {
  up "$OU/api/version" && return 0
  OLLAMA_KV_CACHE_TYPE=q4_0 OLLAMA_FLASH_ATTENTION=1 nohup ollama serve >>"$H/logs/ollama.log" 2>&1 &
  echo $! >"$H/ollama.pid"; i=0
  while ! up "$OU/api/version"; do i=$((i+1)); [ $i -gt 30 ] && { echo "Ollama did not start, see $H/logs/ollama.log"; return 1; }; sleep 1; done
}
case "${1:-help}" in
  start)
    ensure_ollama || exit 1
    if up "http://127.0.0.1:$PORT/health"; then echo "lamellar is already running: http://localhost:$PORT/"; exit 0; fi
    cd "$H/app" && nohup "$PY" -m lamellar.entry.server --config "$H/entry.json" >>"$H/logs/entry.log" 2>&1 &
    echo $! >"$H/entry.pid"; i=0
    while ! up "http://127.0.0.1:$PORT/health"; do i=$((i+1)); [ $i -gt 20 ] && { echo "did not start, see: lamellar logs"; exit 1; }; sleep 1; done
    echo "lamellar is running: http://localhost:$PORT/" ;;
  stop)
    [ -f "$H/entry.pid" ] && kill "$(cat "$H/entry.pid")" 2>/dev/null; rm -f "$H/entry.pid"; echo "stopped" ;;
  restart) "$0" stop; sleep 1; exec "$0" start ;;
  status)  if up "http://127.0.0.1:$PORT/health"; then echo "running on http://localhost:$PORT/"; else echo "not running"; exit 1; fi ;;
  logs)    tail -n "${2:-50}" "$H/logs/entry.log" ;;
  doctor)  shift; cd "$H/app" && exec "$PY" -m lamellar.doctor --home "$H" "$@" ;;
  run)     shift; exec "$PY" "$H/app/cli.py" --config "$H/worker.json" "$@" ;;
  open)    url="http://localhost:$PORT/"; (command -v xdg-open >/dev/null && xdg-open "$url") || (command -v open >/dev/null && open "$url") || echo "$url" ;;
  *) echo "usage: lamellar start|stop|restart|status|logs [n]|doctor [--no-smoke]|run \"task\"|open"; [ "${1:-help}" = help ] ;;
esac
EOF
chmod +x "$BIN_DIR/lamellar"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not on your PATH. Add this to your shell profile: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;; esac

# ---------- 6. start + Open WebUI + browser ----------
"$BIN_DIR/lamellar" restart   # restart, so a re-run picks up the new code

URL="http://localhost:$PORT/"
if [ "$UI" != builtin ]; then
  say "Starting Open WebUI in Docker on port $WEBUI_PORT"
  DOCKER=docker; docker info >/dev/null 2>&1 || { need_root && DOCKER="$SUDO docker"; }
  if $DOCKER ps -a --format '{{.Names}}' | grep -qx lamellar-webui; then $DOCKER rm -f lamellar-webui >/dev/null; fi
  $DOCKER run -d --name lamellar-webui --restart unless-stopped -p "$WEBUI_PORT:8080" \
    --add-host=host.docker.internal:host-gateway \
    -e OPENAI_API_BASE_URL="http://host.docker.internal:$PORT/v1" -e OPENAI_API_KEY="$KEY" \
    -e ENABLE_OLLAMA_API=false -e WEBUI_AUTH=false \
    -v lamellar-webui:/app/backend/data ghcr.io/open-webui/open-webui:main >/dev/null \
    || warn "Open WebUI did not start; the built-in interface still works at $URL"
  [ "$UI" = openwebui ] && URL="http://localhost:$WEBUI_PORT/"
  say "Open WebUI: http://localhost:$WEBUI_PORT/ (first start takes a minute)"
fi

if [ "${LAMELLAR_NO_BROWSER:-}" != 1 ]; then
  if [ "$OS" = Darwin ]; then open "$URL" 2>/dev/null || true
  elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && have xdg-open; then xdg-open "$URL" >/dev/null 2>&1 || true
  fi
fi

# ---------- 7. doctor ----------
say "Running lamellar doctor"
"$BIN_DIR/lamellar" doctor || warn "doctor found problems; the lines marked FAIL say how to fix them."

say "Done. Open $URL"
say "Commands: lamellar start | stop | status | logs | doctor | run \"task\""
