# lamellar

Armor for small local models: a small, readable, fully offline agent toolkit with checks before delivery. (A lamellar armour is made of many small plates.)

Status: in development (alpha). Provided as is, with no guarantee of support.

## Install

```
curl -fsSL https://lamellar.org/install.sh | sh        # Linux, macOS
irm https://lamellar.org/install.ps1 | iex             # Windows (PowerShell)
```

It installs Ollama if needed, picks a model for your GPU, opens `http://localhost:8102/` and runs `lamellar doctor`.
Details: `docs/INSTALL.md`.

## Layout

- `lamellar/doctor.py`  `lamellar doctor`: checks the install and says how to fix what is wrong

- `lamellar/core/`  loop, tools, command execution
- `lamellar/armor/`  requirements store, checks, bounce-back, guard
- `lamellar/entry/`  web interface, queueing, conversations
- `cli.py`  command-line entry point
- `EVENTS.md`  event format between the layers

Hard rule: `core` and `armor` must not import `entry`.

Configuration uses JSON (`config.example.json`): the machine runs Python 3.10, which has no built-in TOML reader, and the core avoids any external dependency.

## License

Apache-2.0. Copyright 2026 The lamellar Authors. See `LICENSE`.
