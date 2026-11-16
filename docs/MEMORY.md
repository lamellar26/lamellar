# Memory and self-improvement

Set `"memory": true` to enable. Off by default.

## Two memory files

One set per bot + chat, stored in `<state_dir>/memory/<bot>-<chat>/`:

- `USER.md` (limit 1,500 characters): who the user is, preferences, usual writing style
- `NOTES.md` (limit 2,500 characters): facts and lessons learned on the job

Both are added to the system instructions when a job starts. The limits are deliberate: they force merging and pruning so the memory does not grow messier over time.

## Gatekeeping

- **Never-remember list**: passwords, API keys, ID numbers, bank accounts, phone numbers, emails and specific amounts are refused on a match. `memory_deny` can add your own regexes.
- **Re-read before edit**: a change must carry the version fingerprint from when it was read; it is refused if someone edited in between.
- **User lock**: lines starting with 🔒 can only be changed by the user (`/remember`).
- **Archive, never delete**: removed lines move to `ARCHIVE.md`; a backup is made before every write.

## Review after answering

Only after delivery (`done` has already been sent) does the review of this conversation run, proposing at most 3 changes, each of which must pass the gatekeeping.
Triggers: a delivery check bounced this time, or every `review_every` turns. The user does not wait.

## Effectiveness scoring

`lamellar.memory.scoring.rule_scores(armor)`: for each requirement, how many times it was used, how many times it was followed on the first delivery, how many times it was fixed after a bounce, and how many times it still failed at the end. Each is classed as "effective / needs bounces / failing / unused".
It is computed from delivery check results, not just usage counts.
`scoring.weekly(armor)` gives three weekly numbers: share fully followed, average bounces, and number of repeated corrections.
