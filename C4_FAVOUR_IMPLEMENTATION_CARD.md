# C4-v3 — Favour evidence / automatic blacklist

- Favour persists operational moderation controls and exposes administrator management. **Adapt** a narrow local settlement blacklist only.
- Relation Arc rejects any blanket message interception: blacklist is evaluated only after a valid Relation Arc protocol settlement candidate exists.

## Invariants
1. `auto_blacklist.enabled` defaults false.
2. The blacklist is persistent SQLite and only applies to Relation Arc settlement—not normal messages or unrelated plugins.
3. A blacklisted accepted-protocol turn is redacted, stripped normally, then produces no account/binding/timed-safety mutation and no protocol-health penalty.
4. Bot-admin-only private commands list/clear exact canonical identities; group calls cannot expose target or existence.
5. Criteria are deterministic: configured count of accepted settlement events in scope, then blacklist that account.
