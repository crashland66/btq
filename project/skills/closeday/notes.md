# Closeday skill

v3 is the post-vault-cutover version. Key changes from v2:

- Step 1 reads today's processed entity activity via `btq export-vault-today` instead
  of reading `OperationalVault/Journal/YYYY-MM-DD.md` (which is frozen after cutover).
- Shift report writes to `Grimoire/Shift Reports/YYYY-MM-DD-shift-report.md` instead
  of `OperationalVault/Journal/YYYY-MM-DD-shift-report.md`.
- For rollover, prior shift reports are read from `Grimoire/Shift Reports/` (new
  location) as well as `OperationalVault/Journal/` (frozen vault, for pre-cutover reports).
- The outbox and Grimoire journal reads are unchanged.
