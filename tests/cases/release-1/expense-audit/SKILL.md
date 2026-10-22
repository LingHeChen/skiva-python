---
name: expense-audit
description: >-
  Audit synthetic travel expenses using regional limits,
  currency rules and a reproducible local calculator.
---
# Expense audit
1. Read references/policy.json and references/currency.json from this snapshot.
2. Read references/regions/上海.md for the regional rule.
3. Apply policy to assets/receipts.json using scripts/evaluate.py.
4. Render templates/report.md. Report the snapshot and evidence paths.
These are synthetic test rules, not financial advice.
