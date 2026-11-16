# Event format (entry layer <-> agent worker process)

Only one JSON object per line is passed between the two layers. The first version has only the types below, and no others.

## Entry layer to agent (command-line arguments)

```
python3 cli.py --model <model name> --endpoint <url> --context <length> [--session <id>] [--config <file>] "<task>"
```
The working directory is the current directory at launch.

## Agent reports (stdout, line by line)

- `{"type":"start","session":"…","model":"…","cwd":"…","version":"…"}`
- `{"type":"step_start","n":1}`  start of one model call
- `{"type":"text","text":"…"}`  text the model said (emitted once a paragraph is complete, not character by character)
- `{"type":"tool_start","id":"t1","name":"bash","args":{"command":"ls"}}`
- `{"type":"tool_end","id":"t1","name":"bash","ok":true,"secs":0.3,"preview":"first 300 characters"}`
- `{"type":"compact","ok":true,"before":66000,"after":14000,"dropped":41}`  the conversation got too long and the middle was compacted into a summary
- `{"type":"check","passed":false,"bounce":1,"failed":[{"rule_id":3,"why":"…"}]}`  delivery check result; if it failed and attempts remain, the answer is bounced back for a rewrite
- `{"type":"ask","id":"a1","question":"…"}`  asks a human; the agent pauses for the answer
- `{"type":"done","ok":true,"answer":"final answer","steps":3,"tools":2,"secs":12.5,"usage":{"prompt":1234,"completion":56},"checks":null}`
- `{"type":"error","message":"…"}`  always followed by a `done` (ok=false)

- `{"type":"memory","changes":[{"op":"add","file":"USER.md","result":"applied"}]}`  background review after the answer (emitted after `done`; the entry layer can reply to the user on `done` without waiting)

Every run has exactly one `done`; with memory enabled, a `memory` event may follow `done`.

## Entry layer to agent (stdin, line by line)

- `{"type":"answer","id":"a1","allow":true,"text":"…"}`  allow = approve or not; text is relayed to the model
- `{"type":"cancel"}`

In a single CLI run stdin is empty: an `ask` that nobody can answer is always treated as a denial.

## Cancel

The entry layer just kills the whole worker process group; on SIGTERM the agent first kills all commands it started.
