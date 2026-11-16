# Install

## One line

Linux and macOS:

```
curl -fsSL https://lamellar.org/install.sh | sh
```

No curl (fresh Ubuntu desktop): `wget -qO- https://lamellar.org/install.sh | sh`

Windows: not supported yet (planned, via WSL2).

When it finishes, your browser opens `http://localhost:8102/`. Ask it something.

## What the installer does

1. Checks for Python 3.10+ (installs it with apt/dnf/pacman/brew if missing).
2. Installs [Ollama](https://ollama.com) with its official installer if missing.
3. Reads your GPU memory and picks a model (table below).
4. Downloads the model and builds a local copy with the right context length (`PARAMETER num_ctx`).
5. Puts the code in `~/.lamellar/app` and writes `~/.lamellar/worker.json` and `~/.lamellar/entry.json`.
6. Starts the web entry, optionally starts Open WebUI in Docker, and opens your browser.
7. Runs `lamellar doctor`.

Running it again upgrades the code and keeps a backup of your config (`*.json.bak`).

It never installs GPU drivers. If you have an NVIDIA card without a driver, it says so, uses the CPU,
and tells you how to install the driver.

## Which model

Provisional table, to be replaced by measured results in `docs/MODELS.md`:

```
GPU memory          model          context
16 GB or more       qwen3.8:27b    96k   (needs 4-bit KV cache, set by the installer)
8 to 15 GB          qwen3.5:9b     32k
4 to 7 GB           qwen3.5:4b     32k
no GPU / < 4 GB     qwen3.5:2b     16k   (works, but slow)
Apple Silicon       27b at 32 GB+, 9b at 16 GB+, else 4b
```

Override: `curl -fsSL https://lamellar.org/install.sh | LAMELLAR_MODEL=qwen3.5:9b sh`
(put the variables after the `|`, in front of `sh`; in front of `curl` they only reach curl)

## Already have Ollama and a model

Use the same line and name your model:

```
curl -fsSL https://lamellar.org/install.sh | LAMELLAR_MODEL=qwen3.8:27b-96k sh
```

- Ollama is found and left alone (no reinstall, no service changes).
- A model that is already installed is not downloaded again, even one you built yourself with a Modelfile.
- The context length is taken from the model's `num_ctx`. If it has none, 32k is used.
- Add `LAMELLAR_CTX=65536` to ask for more than the model's `num_ctx`: the installer then builds a copy
  `lamellar-<model>:<n>k` with the larger `num_ctx`. Your model is not changed.
- Ollama on another address: add `OLLAMA_HOST=127.0.0.1:11500`.

## Options

```
LAMELLAR_UI=builtin|openwebui|both   interface (default: ask; built-in if not interactive)
LAMELLAR_MODEL / LAMELLAR_CTX        force model / context length
LAMELLAR_PORT=8102                   web entry port
LAMELLAR_HOME=~/.lamellar            config, logs, code
LAMELLAR_SRC=<url .tar.gz | dir>     where to get the code
LAMELLAR_YES=1                       never ask
LAMELLAR_NO_BROWSER=1                do not open a browser
LAMELLAR_NO_SMOKE=1                  doctor skips the model test (does not load the model)
OLLAMA_HOST=127.0.0.1:11434          where Ollama listens
```

**Open WebUI**: needs Docker. The installer runs it as container `lamellar-webui` on port 3000,
already connected to lamellar's `/v1`, protected by a random key in `entry.json`.

## Commands

```
lamellar start | stop | restart | status
lamellar logs [n]
lamellar doctor [--no-smoke]
lamellar run "task"        one task in the current directory, events printed as JSON lines
lamellar open
```

## lamellar doctor

Checks Python, both config files, that Ollama answers, that the model is installed and its `num_ctx`
is at least the configured context, the GPU, that the web entry is running, and asks the model one
question. Every failed line is followed by `fix:` telling you what to do. Exit code = number of failures.

## Uninstall

```
lamellar stop
rm -rf ~/.lamellar ~/.local/bin/lamellar
docker rm -f lamellar-webui          # if you chose Open WebUI
ollama rm lamellar-<model>           # the models are Ollama's; remove them there
```
