# Task maintenance

[Domain guide](SKILL.md)

`find_tasks` returns candidates, not an authorization. Verify the original Task,
IDs and index generation before acting. Reads never initialize or repair state.

## Storage

Tasks live in one private SQLite database under the state directory. Do not copy
a live database alone or delete its WAL. There is no migration, reset, rollback
writer or owner takeover.

## Legacy checkpoints

`save_task` uses expected revision zero for creation; identical retries are
unchanged and anchors are additive. Keep short goals, exclusions, authority,
original IDs, outcomes and next action (no secrets, transcripts or bulk logs).

After a lost reply, read the original Task, change or execution ID; never
replay uncertain effects. `resource_busy` before launch is known-no-effect
only with the original retained rejection and absent execution receipt.
Missing locks, databases and corrupt records never authorize reinitialization.
