import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
policy = json.loads((root / "references/policy.json").read_text())
rates = json.loads((root / "references/currency.json").read_text())
receipts = json.loads((root / "assets/receipts.json").read_text())
total = sum(r["amount"] * rates[r["currency"]] for r in receipts)
print(
    json.dumps({"revision": policy["revision"], "total": total, "approved": total <= policy["cap"]})
)
