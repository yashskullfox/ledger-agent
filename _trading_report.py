"""One-shot trading info report."""
import sqlite3
from decimal import Decimal

conn = sqlite3.connect("data/db/financials.db")
conn.row_factory = sqlite3.Row

sep = "=" * 65

print(sep)
print("  TRADING INFO REPORT  (development <- Improvement-1 merged)")
print(sep)

# ── Realised trades ───────────────────────────────────────────────
print("\n── Realised Trading Transactions ──────────────────────────────")
net = Decimal("0")
for code, label in [("4010", "Realised Trading Gains"), ("5070", "Realised Trading Losses")]:
    rows = conn.execute("SELECT amount FROM transactions WHERE coa_code=?", (code,)).fetchall()
    total = sum(Decimal(str(r["amount"])) for r in rows)
    net += total
    sign = "+" if total >= 0 else ""
    print(f"  [{code}] {label:30} {len(rows):3} entries  {sign}${float(total):>12,.2f}")
print(f"  Net G/L captured (Jun–Nov 2024):  ${float(net):>+12,.2f}")
print(f"  2024.txt target  (full year):    +$     6,042.00")
gap = Decimal("6042.00") - net
print(f"  Gap (missing Jan-May + Dec stmts): ${float(gap):>+12,.2f}")

# ── Dividends ────────────────────────────────────────────────────
print("\n── Dividend Income [4021] ─────────────────────────────────────")
rows = conn.execute("SELECT amount FROM transactions WHERE coa_code='4021'").fetchall()
total = sum(Decimal(str(r["amount"])) for r in rows)
ok = "OK" if abs(total - Decimal("37")) < Decimal("1") else "review"
print(f"  {len(rows)} entries  ${float(total):,.2f}  (target $37.00)  [{ok}]")

# ── Margin interest ───────────────────────────────────────────────
print("\n── Margin Interest Expense [5030] ─────────────────────────────")
rows = conn.execute("SELECT amount FROM transactions WHERE coa_code='5030'").fetchall()
total = sum(Decimal(str(r["amount"])) for r in rows)
print(f"  {len(rows)} entries  ${float(total):,.2f}  (target -$1,390.00)")

# ── COA breakdown ─────────────────────────────────────────────────
print("\n── Full COA Breakdown ─────────────────────────────────────────")
rows = conn.execute("""
    SELECT t.coa_code, c.name, COUNT(*) cnt, ROUND(SUM(t.amount),2) total
    FROM transactions t LEFT JOIN coa c ON c.code = t.coa_code
    GROUP BY t.coa_code ORDER BY t.coa_code
""").fetchall()
grand = Decimal("0")
for r in rows:
    total = Decimal(str(r["total"]))
    grand += total
    sign = "+" if total >= 0 else ""
    name = r["name"] or "(unmapped — check COA seed)"
    print(f"  [{r['coa_code']:5}] {name:35} {r['cnt']:4}  {sign}${float(total):>12,.2f}")
print(f"  {'':6} {'NET':35} {'':4}   ${float(grand):>12,.2f}")

# ── Transfers & health ────────────────────────────────────────────
print("\n── Transfers & Data Quality ───────────────────────────────────")
xfer = conn.execute(
    "SELECT COUNT(*), ROUND(SUM(amount),2) FROM transactions WHERE is_transfer=1"
).fetchone()
bad = conn.execute(
    "SELECT COUNT(*) FROM transactions WHERE is_transfer=1 AND (coa_code='' OR coa_code IS NULL)"
).fetchone()[0]
unc = conn.execute(
    "SELECT COUNT(*) FROM transactions WHERE coa_code='' OR coa_code IS NULL"
).fetchone()[0]
print(f"  Transfers:       {xfer[0]} txns  ${float(xfer[1] or 0):,.2f}")
print(f"  Missing coa on xfers: {bad}  {'OK' if bad == 0 else 'FIX NEEDED'}")
print(f"  Unclassified:    {unc}  {'OK' if unc == 0 else 'FIX NEEDED'}")

print("\n" + sep)
conn.close()
