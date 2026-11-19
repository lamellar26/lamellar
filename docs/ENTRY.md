# Entry layer

The entry layer only handles sending and receiving; it does no work itself. It takes a user message, starts an agent worker process (`cli.py`), reads its events (`EVENTS.md`) and pushes them back to the user in plain language.

## Two UIs, use either or both

- **Built-in UI**: open `http://<host>:<port>/` in a browser. A single HTML file (`lamellar/entry/ui.html`); nothing to install. It has a conversation list (stored in the browser), file upload, a work log, stop, and a view of memory. If `api_key` is set, enter it once via "Set password" at the bottom left.
- **Open WebUI or any other OpenAI-compatible UI**: connect to `http://<host>:<port>/v1`.
- Both can run at the same time and share the same queue. To use only an external UI, set `"ui": false`.

Built-in UI only: `POST /v1/lamellar/upload` (headers `X-Filename`, `X-Lamellar-Chat-Id`; size limit `upload_max_mb`) saves the file into the conversation's `uploads/`. When submitting a task, the body carries `lamellar_files`; only files that actually exist in that conversation's `uploads/` are accepted.

## Web endpoint (OpenAI-compatible)

```
python3 -m lamellar.entry.server --config entry.json
```

- `GET /health`, `GET /v1/models`, `POST /v1/chat/completions` (streaming and non-streaming)
- In Open WebUI, add an OpenAI connection pointing at `http://<host>:<port>/v1` and it works
- One job at a time; the rest queue, and streaming shows your queue position
- Each conversation (`X-OpenWebUI-Chat-Id`) has its own working directory and session file, and can be continued
- When the user presses stop (disconnects), the whole worker process group is killed
- Files uploaded through Open WebUI are downloaded into the conversation's `uploads/`; files are only fetched from the configured `webui_url`
- Open WebUI background chores (title renaming etc., starting with `### Task:`) do not start an agent

In-chat commands: `/new` starts a new conversation (the old one is archived, not deleted), `/memory` shows memory, `/remember <text>` adds a line yourself and locks it.

## Configuration

`port`, `bind`, `api_key`, `model_name`, `worker_config` (core config file), `workspace`, `webui_url`, `max_minutes`, `bot`, `state_dir`, `ui` (default true), `upload_max_mb` (default 50).

## Writing another entry point

`lamellar.entry.server.Entry` can be reused: `Entry(cfg).run(chat, task, on_line, alive)` already covers queueing, starting the worker process, turning progress into plain language, and stopping. A new entry point only needs to handle sending and receiving.

Approvals: when a step hits a guard zone (sudo, system settings, scheduling, services) the worker asks the entry layer. Pass `on_ask(question) -> (allow, text)` as a fifth argument to `run` and answer it however suits the interface (for example two buttons). It runs in its own thread, so it may wait for a person; the worker gives up after `ask_timeout` seconds (default 600), so answer sooner than that. Without `on_ask`, nobody can answer and every such step is denied, which is what the web entry does.
