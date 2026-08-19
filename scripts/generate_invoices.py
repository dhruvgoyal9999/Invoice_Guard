"""
Generate the 21 test invoice PDFs.

    python -m scripts.generate_invoices

Four visually distinct templates across eight vendors. The templates differ in
label wording, table structure and how tax is presented, so the extractor faces
genuine layout variety rather than one format repeated 21 times.

Clean PDFs land in data/invoices/clean/. Invoices marked "scan" are ALSO
rendered here -- scripts/degrade_to_scan.py turns those into scanned versions.
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from src import config
from scripts.invoice_specs import INVOICES, VENDORS

W, H = A4
GREY = (0.45, 0.45, 0.45)
FAINT = (0.72, 0.72, 0.72)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def money(n: float) -> str:
    """Indian grouping, e.g. 2025000 -> '20,25,000.00'."""
    whole = int(n)
    frac = int(round((n - whole) * 100))
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{s}.{frac:02d}"


def totals(spec):
    sub = spec["subtotal"]
    tax = sub * spec["gst_rate"] / 100
    return sub, tax, sub + tax


def rule(c, y, x0=18 * mm, x1=W - 18 * mm, width=0.6, col=(0, 0, 0)):
    c.setStrokeColorRGB(*col)
    c.setLineWidth(width)
    c.line(x0, y, x1, y)


# ---------------------------------------------------------------------------
# TEMPLATE A -- "classic"   (V-001 Sharma, V-006 Coastal)
# Centred letterhead, boxed meta block, IGST line.
# Labels: Invoice No. / Invoice Date / Purchase Order / Taxable Value
# ---------------------------------------------------------------------------

def tpl_classic(c, spec, v):
    sub, tax, total = totals(spec)
    y = H - 22 * mm

    c.setFont("Helvetica-Bold", 15)
    c.drawCentredString(W / 2, y, v["name"])
    y -= 6 * mm
    c.setFont("Helvetica", 8.5)
    c.setFillColorRGB(*GREY)
    for line in v["address"]:
        c.drawCentredString(W / 2, y, line)
        y -= 4 * mm
    c.drawCentredString(W / 2, y, f"GSTIN: {v['gstin']}")
    c.setFillColorRGB(0, 0, 0)

    y -= 8 * mm
    rule(c, y, width=1.1)
    y -= 7 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(W / 2, y, "TAX INVOICE")

    y -= 11 * mm
    box_top = y
    box_h = 22 * mm if "period" not in spec else 28 * mm
    c.setStrokeColorRGB(*GREY)
    c.setLineWidth(0.5)
    c.rect(18 * mm, box_top - box_h, W - 36 * mm, box_h)

    ty = box_top - 6 * mm
    c.setFont("Helvetica-Bold", 9)
    if spec.get("illegible_invoice_number"):
        c.drawString(23 * mm, ty, "Invoice No.")
        c.setFillColorRGB(*FAINT)
        c.setFont("Helvetica", 7.5)
        c.drawString(52 * mm, ty, spec["invoice_number"])
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 9)
    else:
        c.drawString(23 * mm, ty, "Invoice No.")
        c.setFont("Helvetica", 9)
        c.drawString(52 * mm, ty, spec["invoice_number"])
        c.setFont("Helvetica-Bold", 9)
    c.drawString(115 * mm, ty, "Invoice Date")
    c.setFont("Helvetica", 9)
    c.drawString(148 * mm, ty, spec["date"])

    ty -= 6 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(23 * mm, ty, "Purchase Order")
    c.setFont("Helvetica", 9)
    c.drawString(52 * mm, ty, spec["po_ref"])
    c.setFont("Helvetica-Bold", 9)
    c.drawString(115 * mm, ty, "Place of Supply")
    c.setFont("Helvetica", 9)
    c.drawString(148 * mm, ty, "Maharashtra (27)")

    if "period" in spec:
        ty -= 6 * mm
        c.setFont("Helvetica-Bold", 9)
        c.drawString(23 * mm, ty, "Service Period")
        c.setFont("Helvetica", 9)
        c.drawString(52 * mm, ty, f"{spec['period'][0]} to {spec['period'][1]}")

    y = box_top - box_h - 12 * mm
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(20 * mm, y, "S.No")
    c.drawString(32 * mm, y, "Description of Services")
    c.drawRightString(130 * mm, y, "Qty")
    c.drawRightString(158 * mm, y, "Rate")
    c.drawRightString(W - 20 * mm, y, "Amount (Rs.)")
    y -= 2.5 * mm
    rule(c, y)

    y -= 6 * mm
    c.setFont("Helvetica", 8.5)
    for i, (desc, qty, rate) in enumerate(spec["lines"], 1):
        c.drawString(20 * mm, y, str(i))
        c.drawString(32 * mm, y, desc[:60])
        c.drawRightString(130 * mm, y, str(qty))
        c.drawRightString(158 * mm, y, money(rate))
        c.drawRightString(W - 20 * mm, y, money(qty * rate))
        y -= 6 * mm

    y -= 1 * mm
    rule(c, y)
    y -= 7 * mm
    for label, val, bold in [
        ("Taxable Value", sub, False),
        (f"IGST @ {spec['gst_rate']}%", tax, False),
        ("Total Payable", total, True),
    ]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.5 if bold else 9)
        c.drawRightString(158 * mm, y, label)
        c.drawRightString(W - 20 * mm, y, money(val))
        y -= 6 * mm

    c.setFont("Helvetica", 7.5)
    c.setFillColorRGB(*GREY)
    c.drawString(18 * mm, 22 * mm, "Payment due within 30 days of invoice date.")
    c.drawString(18 * mm, 18 * mm, "This is a computer generated invoice.")


# ---------------------------------------------------------------------------
# TEMPLATE B -- "modern"   (V-002 Meridian, V-005 Vertex)
# Left-aligned, no box, two-column meta, "Bill Number" / "Dated" / "Ref: PO"
# ---------------------------------------------------------------------------

def tpl_modern(c, spec, v):
    sub, tax, total = totals(spec)

    c.setFillColorRGB(0.13, 0.20, 0.30)
    c.rect(0, H - 30 * mm, W, 30 * mm, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(18 * mm, H - 14 * mm, v["name"])
    c.setFont("Helvetica", 8)
    c.drawString(18 * mm, H - 20 * mm, ", ".join(v["address"]))
    c.drawString(18 * mm, H - 25 * mm, f"GSTIN {v['gstin']}")
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(W - 18 * mm, H - 18 * mm, "INVOICE")
    c.setFillColorRGB(0, 0, 0)

    y = H - 44 * mm
    meta = [
        ("Bill Number", spec["invoice_number"]),
        ("Dated", spec["date"]),
        ("Ref: PO", spec["po_ref"]),
    ]
    if "period" in spec:
        meta.append(("Period Covered", f"{spec['period'][0]} - {spec['period'][1]}"))

    for label, val in meta:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(*GREY)
        c.drawString(18 * mm, y, label.upper())
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(55 * mm, y, val)
        y -= 6.5 * mm

    y -= 6 * mm
    c.setFillColorRGB(0.93, 0.94, 0.96)
    c.rect(18 * mm, y - 2 * mm, W - 36 * mm, 8 * mm, fill=1, stroke=0)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(21 * mm, y + 0.5 * mm, "Particulars")
    c.drawRightString(132 * mm, y + 0.5 * mm, "Units")
    c.drawRightString(160 * mm, y + 0.5 * mm, "Unit Price")
    c.drawRightString(W - 21 * mm, y + 0.5 * mm, "Value")

    y -= 10 * mm
    c.setFont("Helvetica", 8.5)
    for desc, qty, rate in spec["lines"]:
        c.drawString(21 * mm, y, desc[:62])
        c.drawRightString(132 * mm, y, str(qty))
        c.drawRightString(160 * mm, y, money(rate))
        c.drawRightString(W - 21 * mm, y, money(qty * rate))
        y -= 6.5 * mm

    y -= 3 * mm
    rule(c, y, x0=110 * mm, col=GREY)
    y -= 7 * mm
    c.setFont("Helvetica", 9)
    c.drawRightString(160 * mm, y, "Sub Total")
    c.drawRightString(W - 21 * mm, y, money(sub))
    y -= 6 * mm
    c.drawRightString(160 * mm, y, f"GST {spec['gst_rate']}%")
    c.drawRightString(W - 21 * mm, y, money(tax))
    y -= 3 * mm
    rule(c, y, x0=110 * mm, col=GREY)
    y -= 7 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(160 * mm, y, "Amount Due")
    c.drawRightString(W - 21 * mm, y, f"Rs. {money(total)}")

    c.setFont("Helvetica", 7.5)
    c.setFillColorRGB(*GREY)
    c.drawString(18 * mm, 20 * mm, "Kindly quote the bill number on remittance advice.")


# ---------------------------------------------------------------------------
# TEMPLATE C -- "industrial"   (V-003 Nexus, V-008 Quantum)
# All-caps header, full grid table, "Document Ref" / "Issue Date" / "P.O. Number"
# ---------------------------------------------------------------------------

def tpl_industrial(c, spec, v):
    sub, tax, total = totals(spec)
    y = H - 20 * mm

    c.setFont("Helvetica-Bold", 13)
    c.drawString(18 * mm, y, v["name"].upper())
    y -= 5.5 * mm
    c.setFont("Helvetica", 8)
    for line in v["address"]:
        c.drawString(18 * mm, y, line)
        y -= 4 * mm
    c.drawString(18 * mm, y, f"GST Registration: {v['gstin']}")

    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(W - 18 * mm, H - 20 * mm, "GST TAX INVOICE")

    y -= 9 * mm
    rule(c, y, width=1.4)

    y -= 8 * mm
    c.setFont("Helvetica", 9)
    for label, val in [
        ("Document Ref", spec["invoice_number"]),
        ("Issue Date", spec["date"]),
        ("P.O. Number", spec["po_ref"]),
    ]:
        c.setFont("Helvetica", 9)
        c.drawString(18 * mm, y, f"{label}:")
        c.setFont("Helvetica-Bold", 9)
        c.drawString(48 * mm, y, val)
        y -= 5.5 * mm

    y -= 5 * mm
    tbl_top = y
    col_x = [18 * mm, 30 * mm, 118 * mm, 145 * mm, W - 18 * mm]
    rows = len(spec["lines"])
    row_h = 7 * mm
    tbl_bot = tbl_top - (rows + 1) * row_h

    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.5)
    for x in col_x:
        c.line(x, tbl_top, x, tbl_bot)
    for i in range(rows + 2):
        yy = tbl_top - i * row_h
        c.line(col_x[0], yy, col_x[-1], yy)

    c.setFont("Helvetica-Bold", 8)
    hy = tbl_top - 4.8 * mm
    c.drawString(20 * mm, hy, "Item")
    c.drawString(32 * mm, hy, "Description")
    c.drawRightString(143 * mm, hy, "Qty x Rate")
    c.drawRightString(W - 20 * mm, hy, "Value (INR)")

    c.setFont("Helvetica", 8)
    for i, (desc, qty, rate) in enumerate(spec["lines"], 1):
        ry = tbl_top - (i + 1) * row_h + 2.2 * mm
        c.drawString(21 * mm, ry, f"{i:02d}")
        c.drawString(32 * mm, ry, desc[:56])
        c.drawRightString(143 * mm, ry, f"{qty} x {money(rate)}")
        c.drawRightString(W - 20 * mm, ry, money(qty * rate))

    y = tbl_bot - 9 * mm
    c.setFont("Helvetica", 9)
    c.drawRightString(150 * mm, y, "Net Amount")
    c.drawRightString(W - 18 * mm, y, money(sub))
    y -= 5.5 * mm
    c.drawRightString(150 * mm, y, f"Tax @ {spec['gst_rate']}% (IGST)")
    c.drawRightString(W - 18 * mm, y, money(tax))
    y -= 6.5 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(150 * mm, y, "GRAND TOTAL")
    c.drawRightString(W - 18 * mm, y, money(total))

    c.setFont("Helvetica", 7.5)
    c.setFillColorRGB(*GREY)
    c.drawString(18 * mm, 20 * mm,
                 "Goods once despatched will not be taken back. Subject to jurisdiction.")


# ---------------------------------------------------------------------------
# TEMPLATE D -- "compact"   (V-004 Pinnacle, V-007 Alpine)
# Dense, small type, CGST+SGST split instead of a single IGST line.
# Labels: Inv # / Date / Order Ref / Amount (excl. tax)
# ---------------------------------------------------------------------------

def tpl_compact(c, spec, v):
    sub, tax, total = totals(spec)
    half = tax / 2
    y = H - 18 * mm

    c.setFont("Helvetica-Bold", 11.5)
    c.drawString(18 * mm, y, v["name"])
    c.setFont("Helvetica", 7.5)
    c.drawRightString(W - 18 * mm, y, f"GSTIN {v['gstin']}")
    y -= 4.5 * mm
    c.setFillColorRGB(*GREY)
    c.drawString(18 * mm, y, " | ".join(v["address"]))
    c.setFillColorRGB(0, 0, 0)

    y -= 6 * mm
    rule(c, y, width=0.8)

    y -= 6 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18 * mm, y, "INVOICE")
    c.setFont("Helvetica", 8)
    c.drawString(45 * mm, y, f"Inv #  {spec['invoice_number']}")
    c.drawString(100 * mm, y, f"Date  {spec['date']}")
    c.drawString(140 * mm, y, f"Order Ref  {spec['po_ref']}")

    y -= 8 * mm
    rule(c, y, col=GREY, width=0.4)
    y -= 5 * mm
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(18 * mm, y, "DESCRIPTION")
    c.drawRightString(125 * mm, y, "QTY")
    c.drawRightString(152 * mm, y, "RATE")
    c.drawRightString(W - 18 * mm, y, "AMOUNT")
    y -= 2 * mm
    rule(c, y, col=GREY, width=0.4)

    y -= 5.5 * mm
    c.setFont("Helvetica", 8)
    for desc, qty, rate in spec["lines"]:
        c.drawString(18 * mm, y, desc[:64])
        c.drawRightString(125 * mm, y, str(qty))
        c.drawRightString(152 * mm, y, money(rate))
        c.drawRightString(W - 18 * mm, y, money(qty * rate))
        y -= 5.5 * mm

    y -= 1 * mm
    rule(c, y, x0=100 * mm, col=GREY, width=0.4)
    y -= 6 * mm
    c.setFont("Helvetica", 8.5)
    for label, val in [
        ("Amount (excl. tax)", sub),
        (f"CGST @ {spec['gst_rate'] / 2:g}%", half),
        (f"SGST @ {spec['gst_rate'] / 2:g}%", half),
    ]:
        c.drawRightString(152 * mm, y, label)
        c.drawRightString(W - 18 * mm, y, money(val))
        y -= 5.5 * mm

    rule(c, y + 1 * mm, x0=100 * mm, col=GREY, width=0.4)
    y -= 4 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(152 * mm, y, "TOTAL")
    c.drawRightString(W - 18 * mm, y, f"Rs. {money(total)}")

    c.setFont("Helvetica", 7)
    c.setFillColorRGB(*GREY)
    c.drawString(18 * mm, 18 * mm,
                 "E&OE. Interest @ 18% p.a. applicable on overdue amounts.")


TEMPLATES = {
    "classic": tpl_classic,
    "modern": tpl_modern,
    "industrial": tpl_industrial,
    "compact": tpl_compact,
}


# ---------------------------------------------------------------------------
# DRIVER
# ---------------------------------------------------------------------------

def check_specs() -> None:
    """Refuse to generate if any invoice's line items do not sum to its subtotal."""
    problems = []
    for s in INVOICES:
        calc = sum(q * r for _, q, r in s["lines"])
        if calc != s["subtotal"]:
            problems.append(
                f"  {s['stem']}: lines sum to {calc:,} but subtotal is {s['subtotal']:,}"
            )
    if problems:
        raise SystemExit("Line items do not reconcile:\n" + "\n".join(problems))


def main() -> None:
    check_specs()
    out = config.CLEAN_INVOICE_DIR
    out.mkdir(parents=True, exist_ok=True)

    counts = {}
    for spec in INVOICES:
        v = VENDORS[spec["vendor_id"]]
        path = out / f"{spec['stem']}.pdf"
        c = canvas.Canvas(str(path), pagesize=A4)
        c.setTitle(spec["invoice_number"])
        TEMPLATES[v["template"]](c, spec, v)
        c.showPage()
        c.save()
        counts[v["template"]] = counts.get(v["template"], 0) + 1

    print(f"Generated {len(INVOICES)} invoices in {out}")
    for tpl, n in sorted(counts.items()):
        print(f"  {tpl:<12} {n}")
    scans = [s["stem"] for s in INVOICES if s.get("scan")]
    print(f"\nMarked for scan degradation ({len(scans)}): {', '.join(scans)}")


if __name__ == "__main__":
    main()
