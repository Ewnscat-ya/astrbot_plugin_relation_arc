# C1-v3 — Favour evidence / implementation gate

| Favour source | Favour behavior | Relation Arc decision |
|---|---|---|
| `config_manager.py:104-115` | configurable duration and scope | adapt duration + existing global/session scope |
| `main.py:2199-2211` | trigger records `now + duration` and can refresh expiry | adapt: accepted safety proposal creates/extends an automatic record |
| `main.py:1659-1673,2246-2256` | lazy expiry check on message/query | adapt: every effective safety read normalizes expiry, independent of LLM enabled state |
| `main.py:2713-2782` | administrator cancel/list | adapt: exact canonical scope target; private/Pages only, no group identities |
| `storage.py` | no cold-violence persistence | reject: Relation Arc persists SQLite override across reload |
| `main.py:300-314` | task cleanup | C1 adds no timer; C3 handles task lifecycle |

## Invariants
1. Automatic state is a separate timed override, never a base `accounts.state_json` write.
2. Same-strength proposal renews expiry; weaker proposal cannot lower a stronger effective state.
3. Expiry deletes only the matching automatic row and never writes `normal` to base state.
4. Administrator set/clear is exact `(identity, scope_kind, scope_id)` and cancels timer transactionally.
5. All effective state reads normalize expiry even with LLM settlement disabled.
6. No ordinary chat interception; no group disclosure of automatic state.
7. Migration is non-destructive with protected backup via the store migration path.
