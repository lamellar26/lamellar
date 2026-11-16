# Industry packs

The core knows nothing about any industry. Everything industry-specific lives in a "pack", like a WordPress plugin: one directory per pack, listed in the config to enable it, removed from the config to disable it, and the core never needs to change.

## Enabling and disabling

Config file (JSON):

```
"packs": ["example-pack"],
"packs_dir": ["/path/to/private/packs"]
```

- Enable: add the pack name to `packs`.
- Disable: remove it from `packs`.
- Pack lookup order: the directories listed in `packs_dir`, then the core repo's `packs/`, then `~/.lamellar/packs/`. Keep private packs outside the core repo.

## Pack layout

```
<pack-name>/
  pack.json    name, version, requires_core (which core version is needed), description
  pack.py      optional; register(api) is called if present
  rules/       requirement templates
  skills/      *.md working methods; the first line may be: trigger: <regex>
  tools/       the pack's own code
  tests/       the pack's tests
```

## What register(api) can do

- `api.add_checker(type_name, fn)`: add a rule type. `fn(text, params) -> None (pass) or str (why it failed)`
- `api.add_rule_doc(type_name, description)`: used by correction capture; the model relies on this description to pick a type and fill in params
- `api.add_tool(spec, fn)`: add a tool. `fn(ctx, **params) -> (ok, text)`
- `api.add_guard_rule(rule)`: add an interception rule that runs before a tool
- `api.on(hook_name, fn)`: hooks, see below

## Hooks

Derived backwards from the places the first pack (example-pack) actually uses; nothing unused is designed in advance.

- `before_task(task, ctx) -> str|None`  before the job starts; the returned text is appended to the task
- `before_tool(name, args, ctx) -> (action, info)|None`  before a tool runs; action = allow / deny / ask / rewrite
- `before_delivery(answer, ctx) -> str|None`  before delivery; returning text means the work is not accepted and the text is bounced back to the model to redo (shares the bounce limit with the requirement store)
- `after_delivery(answer, ctx, checks) -> None`  after delivery
- `on_correction(text, ctx) -> None`  when the user corrects something
- `nightly(ctx) -> None`  every night

`ctx` is a dict: `cwd` (working directory), `cfg` (config), `session`, `armor` (requirement store). A pack may put its own things in `ctx` (for example tool results kept for the delivery check).

A hook error does not kill the job: the error is printed to stderr and treated as no response.
