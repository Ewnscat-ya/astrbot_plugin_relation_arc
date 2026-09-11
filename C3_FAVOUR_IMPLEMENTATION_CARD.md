# C3-v3 — Favour evidence / decay scheduler

| Favour source | behavior | Relation Arc adaptation |
|---|---|---|
| `main.py:212,215-236` | creates managed background tasks and cancels/awaits them at shutdown | adapt one scheduler task, explicit cancel/await |
| `main.py:300-314` | catches task errors and keeps lifecycle observable | adapt retry loop with sanitized exception log |
| Favour relationship timestamps | interaction updates drive later mechanics | adapt dedicated `last_interaction`, never overloaded `updated_at` |

## C3 invariants
1. Decay reads only persistent `last_interaction`, updated by accepted Relation Arc settlement—not admin reads/writes.
2. For each dimension decay is `max(floor, value - step)` only where `value > floor`; no value is ever raised to floor.
3. Scheduler is disabled by default; enabled configuration starts one task, catches errors, waits/retries, and terminate cancels/awaits it.
4. Config changes explicitly restart the task through the Pages config write path; no duplicate task.
5. Decay acts only on `_stored_scope_allowed` scopes.
