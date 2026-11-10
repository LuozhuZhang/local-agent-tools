# Verify stages and finish the task

[Domain guide](SKILL.md)

## Verify current acceptance

Read original receipts and actual output/destination evidence; verify the current
source version and applicable tests. Require `status=succeeded`, verified exit and
output for command success. Cancelled or timed-out commands may exit zero. A hash
or partial/redacted page is not proof of complete review, and a historical PASS
cannot accept changed source. Keep application, testing, publication, source merge,
deployment, client refresh and live integration acceptance as separate outcomes.

Use `record_decision` only with verified operations for the current stage, source
hash and plan revision. A retained original receipt from an older plan cannot be
resubmitted as current evidence.
After applicable decisions, explicitly finish:

```json
{"action":"finish","outcome":"completed","acceptance":[{"stage_id":"stage-id","operation_id":"original-operation-id"}],"limitations":""}
```

Include every accepted stage and its original operation references. Use null only
for an accepted non-command decision with no operation evidence. Finish does not
accept `reason`, `summary` or the decision's `evidence` field. A zero exit, claim,
notification or saved checkpoint cannot substitute for task acceptance. Terminal
history stays read-only; follow-up work is a new linked task.

Original operation retries compare intent and read original results, not resubmit.
Missing/corrupt receipts and uncertain publication never authorize replay. Known
supervised identities cannot shed their context to acquire new effects. Receipt
hashes represent bounded semantic observations, not full raw logs or source files. There is no exactly-once
promise for arbitrary shell/API effects.

## Historical results and remaining work

Every unresolved original effect blocks stage acceptance and `finish`. The separate
explicit `abandon` transition records terminal abandonment while keeping those
original outcomes and their resource exclusions unresolved.

Compare every authorized deliverable with actual evidence before the final response.
A saved checkpoint, claim, notice, zero exit or intermediate PR does not complete
a larger request. Continue feasible remaining work; for a real blocker preserve
original records and report the exact evidence, attempted safe recovery, remaining
work and required decision or access. Follow the
[authorized-delivery rule](../overview/SKILL.md#complete-the-authorized-delivery).
