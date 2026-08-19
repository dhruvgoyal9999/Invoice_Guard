"""
The 21 test invoices, as data.

Kept separate from the rendering code so the WHAT (which invoices exist, what
they contain) stays readable without wading through layout code.

Every line item's qty x unit_price must sum exactly to the subtotal. There is a
self-check in generate_invoices.py that refuses to run if any invoice fails it.

Amounts here are in RUPEES for readability, matching the CSV convention.
Conversion to paise happens at the boundary, same as the master data.
"""

# Vendor letterhead details. Template assignment is per vendor -- a real
# vendor's invoices look the same every month.
VENDORS = {
    "V-001": {
        "name": "Sharma Logistics Pvt. Ltd.",
        "address": ["Plot 44, MIDC Industrial Area", "Andheri East, Mumbai 400093"],
        "gstin": "27AABCS1429B1ZX",
        "template": "classic",
    },
    "V-002": {
        "name": "Meridian Consulting Services LLP",
        "address": ["12th Floor, Prestige Tech Park", "Marathahalli, Bengaluru 560103"],
        "gstin": "29AACFM8821K1ZQ",
        "template": "modern",
    },
    "V-003": {
        "name": "NEXUS INDUSTRIAL EQUIPMENT PVT LTD",
        "address": ["Survey 118/2, GIDC Estate", "Vatva, Ahmedabad 382445"],
        "gstin": "24AABCN5567L1ZD",
        "template": "industrial",
    },
    "V-004": {
        "name": "Pinnacle Office Supplies Pvt Ltd",
        "address": ["Unit 7, Okhla Industrial Area Phase II", "New Delhi 110020"],
        "gstin": "07AABCP3312M1ZF",
        "template": "compact",
    },
    "V-005": {
        "name": "Vertex Software Solutions Private Limited",
        "address": ["Level 4, Embassy Golf Links", "Domlur, Bengaluru 560071"],
        "gstin": "29AABCV7745N1ZR",
        "template": "modern",
    },
    "V-006": {
        "name": "Coastal Freight Carriers Pvt. Ltd.",
        "address": ["Warehouse 22, Ennore Port Road", "Chennai 600057"],
        "gstin": "33AAACC2298P1ZJ",
        "template": "classic",
    },
    "V-007": {
        "name": "Alpine Facility Management Pvt Ltd",
        "address": ["Tower C, Cyber Greens, DLF Phase III", "Gurugram 122002"],
        "gstin": "06AADCA9903Q1ZT",
        "template": "compact",
    },
    "V-008": {
        "name": "Quantum Analytics Private Limited",
        "address": ["5th Floor, Salt Lake Sector V", "Kolkata 700091"],
        "gstin": "19AABCQ6614R1ZB",
        "template": "industrial",
    },
}


# seq, filename stem, and everything needed to render one invoice.
# `illegible_invoice_number` renders the number in faint grey so the scan
# degradation step destroys it -- that is EC-4.
INVOICES = [
    # --- Group A: clean happy path -------------------------------------
    {
        "seq": 1, "stem": "01_INV-PIN-4471",
        "invoice_number": "INV-PIN-4471", "vendor_id": "V-004",
        "date": "14/07/2026", "po_ref": "PO-1001", "gst_rate": 18,
        "subtotal": 198500,
        "lines": [
            ("A4 Copier Paper 80gsm (ream)", 250, 400),
            ("Whiteboard Markers - box of 12", 150, 350),
            ("Desk Organiser - 3 tier", 100, 460),
        ],
    },
    {
        "seq": 2, "stem": "02_INV-VTX-2208",
        "invoice_number": "INV-VTX-2208", "vendor_id": "V-005",
        "date": "02/07/2026", "po_ref": "PO-1002", "gst_rate": 18,
        "subtotal": 450000,
        "lines": [
            ("CRM Platform Annual Licence - 50 seats", 50, 7200),
            ("Priority Support Add-on - 12 months", 1, 90000),
        ],
    },
    {
        "seq": 3, "stem": "03_QA-2026-0117",
        "invoice_number": "QA/2026/0117", "vendor_id": "V-008",
        "date": "20/07/2026", "po_ref": "PO-1004", "gst_rate": 18,
        "subtotal": 320000,
        "lines": [
            ("Market research dashboard - design and build", 1, 240000),
            ("Data pipeline configuration", 1, 80000),
        ],
    },
    {
        "seq": 4, "stem": "04_INV-NEX-5590",
        "invoice_number": "INV-NEX-5590", "vendor_id": "V-003",
        "date": "28/06/2026", "po_ref": "PO-1006", "gst_rate": 28,
        "subtotal": 848000,
        "lines": [
            ("Hydraulic Press Unit HP-400", 1, 720000),
            ("Spare Seal Kit - SK400", 4, 22000),
            ("Installation and commissioning", 1, 40000),
        ],
    },
    {
        "seq": 5, "stem": "05_INV-MER-3301",
        "invoice_number": "INV-MER-3301", "vendor_id": "V-002",
        "date": "08/07/2026", "po_ref": "PO-1007", "gst_rate": 18,
        "subtotal": 695000,
        "lines": [
            ("Senior Consultant - professional fees (days)", 95, 5000),
            ("Analyst - professional fees (days)", 110, 2000),
        ],
    },

    # --- Group B: scanned happy path -----------------------------------
    {
        "seq": 6, "stem": "06_INV-VTX-2251",
        "invoice_number": "INV-VTX-2251", "vendor_id": "V-005",
        "date": "16/07/2026", "po_ref": "PO-1008", "gst_rate": 18,
        "subtotal": 698000, "scan": True,
        "lines": [
            ("Custom integration module - development", 1, 520000),
            ("API gateway configuration", 1, 118000),
            ("UAT support (hours)", 30, 2000),
        ],
    },
    {
        "seq": 7, "stem": "07_INV-PIN-4502",
        "invoice_number": "INV-PIN-4502", "vendor_id": "V-004",
        "date": "22/07/2026", "po_ref": "PO-1009", "gst_rate": 18,
        "subtotal": 664000, "scan": True,
        "lines": [
            ("Workstation Desk 1600mm - oak finish", 40, 12400),
            ("Ergonomic Task Chair - mesh back", 40, 4200),
        ],
    },

    # --- Group C: the dual-threshold pair -------------------------------
    {
        "seq": 8, "stem": "08_INV-NEX-5612",
        "invoice_number": "INV-NEX-5612", "vendor_id": "V-003",
        "date": "05/07/2026", "po_ref": "PO-1005", "gst_rate": 18,
        "subtotal": 2025000,
        "lines": [
            ("Conveyor System CS-2000 - 60m run", 1, 1650000),
            ("Motor Drive Unit MDU-15", 3, 95000),
            ("Site installation and alignment", 1, 90000),
        ],
    },
    {
        "seq": 9, "stem": "09_SL-8834",
        "invoice_number": "SL-8834", "vendor_id": "V-001",
        "date": "11/07/2026", "po_ref": "PO-1003", "gst_rate": 18,
        "subtotal": 130000,
        "lines": [
            ("Freight Mumbai to Pune - FTL (trips)", 26, 4200),
            ("Loading and unloading charges (trips)", 26, 800),
        ],
    },

    # --- Group D: EC-1 progressive billing on PO-1010 -------------------
    {
        "seq": 10, "stem": "10_INV-MER-3312",
        "invoice_number": "INV-MER-3312", "vendor_id": "V-002",
        "date": "15/03/2026", "po_ref": "PO-1010", "gst_rate": 18,
        "subtotal": 400000,
        "lines": [("Operating model review - Phase 1 Discovery", 1, 400000)],
    },
    {
        "seq": 11, "stem": "11_INV-MER-3348",
        "invoice_number": "INV-MER-3348", "vendor_id": "V-002",
        "date": "20/05/2026", "po_ref": "PO-1010", "gst_rate": 18,
        "subtotal": 350000,
        "lines": [("Operating model review - Phase 2 Design", 1, 350000)],
    },
    {
        "seq": 12, "stem": "12_INV-MER-3390",
        "invoice_number": "INV-MER-3390", "vendor_id": "V-002",
        "date": "25/07/2026", "po_ref": "PO-1010", "gst_rate": 18,
        "subtotal": 260000,
        "lines": [("Operating model review - Phase 3 Implementation support", 1, 260000)],
    },
    {
        "seq": 13, "stem": "13_INV-MER-3391",
        "invoice_number": "INV-MER-3391", "vendor_id": "V-002",
        "date": "26/07/2026", "po_ref": "PO-1010", "gst_rate": 18,
        "subtotal": 50000,
        "lines": [("Additional workshop facilitation (sessions)", 2, 25000)],
    },

    # --- Group E: EC-2 recurring vs duplicate on PO-1011 -----------------
    {
        "seq": 14, "stem": "14_SL-9012",
        "invoice_number": "SL-9012", "vendor_id": "V-001",
        "date": "16/03/2026", "po_ref": "PO-1011", "gst_rate": 18,
        "subtotal": 45000,
        "period": ("01/03/2026", "15/03/2026"),
        "lines": [("Monthly freight retainer - 01 Mar to 15 Mar 2026", 1, 45000)],
    },
    {
        "seq": 15, "stem": "15_SL-9034",
        "invoice_number": "SL-9034", "vendor_id": "V-001",
        "date": "31/03/2026", "po_ref": "PO-1011", "gst_rate": 18,
        "subtotal": 45000,
        "period": ("16/03/2026", "31/03/2026"),
        "lines": [("Monthly freight retainer - 16 Mar to 31 Mar 2026", 1, 45000)],
    },
    {
        "seq": 16, "stem": "16_SL-9047",
        "invoice_number": "SL-9047", "vendor_id": "V-001",
        "date": "02/04/2026", "po_ref": "PO-1011", "gst_rate": 18,
        "subtotal": 45000,
        "period": ("16/03/2026", "31/03/2026"),
        "lines": [("Monthly freight retainer - 16 Mar to 31 Mar 2026", 1, 45000)],
    },

    # --- Group F: EC-4 unreadable invoice number -------------------------
    {
        "seq": 17, "stem": "17_ILLEGIBLE",
        "invoice_number": "SL-8891", "vendor_id": "V-001",
        "date": "28/07/2026", "po_ref": "PO-1003", "gst_rate": 18,
        "subtotal": 120000, "scan": True, "heavy_degrade": True,
        "illegible_invoice_number": True,
        "lines": [
            ("Freight Mumbai to Pune - FTL (trips)", 24, 4200),
            ("Loading and unloading charges (trips)", 24, 800),
        ],
    },

    # --- Group G: assorted rule triggers ---------------------------------
    {
        "seq": 18, "stem": "18_INV-PIN-4520",
        "invoice_number": "INV-PIN-4520", "vendor_id": "V-004",
        "date": "18/07/2026", "po_ref": "PO-1012", "gst_rate": 18,
        "subtotal": 25000,
        "lines": [("Printer Cartridge HP-410A - black", 20, 1250)],
    },
    {
        "seq": 19, "stem": "19_QA-2026-0131",
        "invoice_number": "QA/2026/0131", "vendor_id": "V-008",
        "date": "19/07/2026", "po_ref": "PO-1013", "gst_rate": 18,
        "subtotal": 150000,
        "lines": [("Customer segmentation study - initial phase", 1, 150000)],
    },
    {
        "seq": 20, "stem": "20_CF-2026-0088",
        "invoice_number": "CF-2026-0088", "vendor_id": "V-006",
        "date": "21/07/2026", "po_ref": "PO-1014", "gst_rate": 18,
        "subtotal": 270000,
        "lines": [
            ("Port handling charges (containers)", 45, 4000),
            ("Last-mile delivery (consignments)", 90, 1000),
        ],
    },
    {
        "seq": 21, "stem": "21_AFM-0034",
        "invoice_number": "AFM-0034", "vendor_id": "V-007",
        "date": "24/07/2026", "po_ref": "PO-1015", "gst_rate": 18,
        "subtotal": 92000,
        "lines": [("Housekeeping services - Q1 2026 contract", 1, 92000)],
    },
]
