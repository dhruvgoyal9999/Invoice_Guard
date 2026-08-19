"""
Rebuild the database from the master CSVs.

Run from the project root:
    python -m scripts.load_masters

Safe to run as often as you like. It drops and recreates every table, so the
CSVs always define the starting state and `already_invoiced` never
double-counts across runs.
"""

from src.money import format_paise
from src.store import get_all_pos, get_all_vendors, load_masters_into_db


def main() -> None:
    counts = load_masters_into_db(verbose=False)

    print()
    print("VENDORS")
    print(f"  {'ID':<7} {'Legal name':<45} {'Approved':<9} Aliases")
    print("  " + "-" * 76)
    for v in get_all_vendors():
        flag = "yes" if v.is_approved else "NO"
        print(f"  {v.vendor_id:<7} {v.legal_name[:44]:<45} {flag:<9} {len(v.aliases)}")

    print()
    print("PURCHASE ORDERS")
    print(f"  {'PO':<9} {'Vendor':<7} {'Total':>16} {'Billed':>16} "
          f"{'Remaining':>16}  {'Status':<10} GST")
    print("  " + "-" * 86)
    for po in get_all_pos():
        print(
            f"  {po.po_number:<9} {po.vendor_id:<7} "
            f"{format_paise(po.po_total_paise):>16} "
            f"{format_paise(po.already_invoiced_paise):>16} "
            f"{format_paise(po.remaining_balance_paise):>16}  "
            f"{po.status.value:<10} {po.expected_gst_rate}%"
        )

    print()
    print(f"  Loaded {counts['vendors']} vendors and "
          f"{counts['purchase_orders']} purchase orders.")
    print()


if __name__ == "__main__":
    main()
