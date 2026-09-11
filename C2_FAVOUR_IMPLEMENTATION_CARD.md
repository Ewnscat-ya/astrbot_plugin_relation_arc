# C2-v3 — Favour evidence / capability gate

- **Favour `main.py:1644-1650`**: allow-list skips unlisted session; block-list skips listed session. **Adapt**: block wins, before Relation Arc account I/O.
- **Favour `main.py:1350-1364`, `permissions.py:51-102`**: superuser is explicit; unsupported adapters do not guess group role. **Adapt**: Relation Arc only trusts host Bot-admin IDs.
- **Favour `main.py:1366-1374`**: normal query permissions are channel-specific. **Adapt**: query toggle applies only after scope gate; group is self-only.
- **Reject Favour broad group tables**: Relation Arc group replies never reveal third-party IDs, values, bindings, or safety.

## C2 invariants
1. Disabled, blocked, or allow-miss event scopes perform no Relation Arc account creation/read mutation through commands or settlement.
2. Admin account/binding mutations are private-only and scope-gated; Bot config recovery remains usable.
3. Group third-party/bulk queries are rejected before target lookup.
4. Pages mutations/listing filter stored session scopes; global Pages management is not treated as a chat-session bypass.
5. Denials reveal no target identity or existence.
