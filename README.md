# lamellar

Armor for small local models: a small, readable, fully offline agent toolkit that checks its work before handing it to you.

Website: https://lamellar.org

Status: in development (alpha). Provided as is, with no guarantee of support.

## Install

```
curl -fsSL https://lamellar.org/install.sh | sh        # Linux, macOS
```

No curl? `wget -qO- https://lamellar.org/install.sh | sh`. Windows is not supported yet.

Already have Ollama and a model? Name it, and nothing is downloaded or reinstalled:

```
curl -fsSL https://lamellar.org/install.sh | LAMELLAR_MODEL=qwen3.8:27b-96k sh
```

It installs Ollama if needed, picks a model for your GPU, opens `http://localhost:8102/` and runs `lamellar doctor`.
Details: `docs/INSTALL.md`.

## What it is

- **An agent that runs on your own machine.** It talks to a local model through [Ollama](https://ollama.com), runs shell commands and edits files in its working directory. Nothing leaves your computer.
- **Armor.** A lamellar is armor made of many small plates. Here the plates are rules: your requirements are stored, every answer is checked against them before delivery, and failed answers are sent back for a rewrite.
- **Guard rails.** Commands that touch credentials, system config, scheduling or services stop and ask a human first. Each command has a time and memory limit.
- **Small and readable.** About two thousand lines of Python, standard library only. Long conversations are compacted inside the core, so it keeps working on a 16 GB card.
- **A web interface included.** Open `http://localhost:8102/`, or connect Open WebUI or any OpenAI-compatible client to `/v1`.

## Which model you get

```
GPU memory       model          context
16 GB or more    qwen3.8 27B    96k
8 to 15 GB       qwen3.5 9B     32k
4 to 7 GB        qwen3.5 4B     32k
no GPU           qwen3.5 2B     16k, slow
```

Provisional; it will be replaced by measured results. Apple Silicon is picked by unified memory. Override with `... | LAMELLAR_MODEL=qwen3.5:9b sh`.

## After installing

```
lamellar start | stop | status | logs
lamellar doctor
lamellar run "summarise the CSV files in this folder"
```

## Layout

- `lamellar/core/`  loop, tools, command execution
- `lamellar/armor/`  requirements store, checks, bounce-back, guard
- `lamellar/entry/`  web interface, queueing, conversations
- `lamellar/doctor.py`  `lamellar doctor`: checks the install and says how to fix what is wrong
- `cli.py`  command-line entry point
- `EVENTS.md`  event format between the layers
- `site/`  the lamellar.org landing page

Hard rule: `core` and `armor` must not import `entry`.

Configuration uses JSON (`config.example.json`): the core supports Python 3.10, which has no built-in TOML reader, and avoids any external dependency.

## License

Apache-2.0. Copyright 2026 The lamellar Authors. See `LICENSE`.
