"""
PO and vendor master data access. Spec Section 7.

This is the ONLY module that touches the database. If another file needs data,
it calls a function here. That keeps storage swappable -- a real ERP could
replace SQLite behind these same functions without anything else changing.

Two conversions happen at this boundary and nowhere else:
  - CSV stores RUPEES (human-readable); everything internal is PAISE.
  - CSV stores aliases pipe-separated; the schema wants a list.
"""

import csv
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config
from .money import rupees_to_paise
from .schemas import POStatus, PurchaseOrder, Vendor


# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------

_SCHEMA = """
-- Drop children before parents, or the foreign key constraint blocks the drop.
DROP TABLE IF EXISTS processed_invoices;
DROP TABLE IF EXISTS purchase_orders;
DROP TABLE IF EXISTS vendors;

CREATE TABLE vendors (
    vendor_id       TEXT PRIMARY KEY,
    legal_name      TEXT NOT NULL,
    aliases         TEXT,
    gstin           TEXT,
    is_approved     INTEGER NOT NULL,
    onboarded_date  TEXT
);

CREATE TABLE purchase_orders (
    po_number               TEXT PRIMARY KEY,
    vendor_id               TEXT NOT NULL,
    vendor_name             TEXT NOT NULL,
    po_date                 TEXT NOT NULL,
    po_total_paise          INTEGER NOT NULL,
    currency                TEXT NOT NULL,
    already_invoiced_paise  INTEGER NOT NULL,
    status                  TEXT NOT NULL,
    expected_gst_rate       INTEGER,
    description             TEXT,
    valid_until             TEXT,
    FOREIGN KEY (vendor_id) REFERENCES vendors(vendor_id)
);

CREATE TABLE processed_invoices (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id            TEXT NOT NULL,
    invoice_number       TEXT,
    po_number            TEXT,
    subtotal_paise       INTEGER NOT NULL,
    invoice_date         TEXT NOT NULL,
    service_period_from  TEXT,
    service_period_to    TEXT,
    decision             TEXT,
    processed_at         TEXT NOT NULL
);

CREATE INDEX idx_pi_vendor ON processed_invoices(vendor_id);
CREATE INDEX idx_po_vendor ON purchase_orders(vendor_id);
"""


# ---------------------------------------------------------------------------
# CONNECTION
# ---------------------------------------------------------------------------

@contextmanager
def _connect():
    """Open a connection with rows accessible by column name."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_loaded() -> None:
    """Fail with a useful message if the database was never built."""
    if not Path(config.DB_PATH).exists():
        raise RuntimeError(
            "Database not found. Run: python -m scripts.load_masters"
        )


# ---------------------------------------------------------------------------
# PARSING HELPERS
# ---------------------------------------------------------------------------

def _parse_bool(raw: str) -> bool:
    return str(raw).strip().upper() in {"TRUE", "1", "YES", "Y"}


def _parse_optional_date(raw: str | None) -> str | None:
    """Validate an ISO date and hand it back as a string, or None if blank."""
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    datetime.strptime(text, "%Y-%m-%d")  # raises if malformed
    return text


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    return int(str(raw).strip())


def _split_aliases(raw: str | None) -> list[str]:
    """CSV stores aliases pipe-separated so commas do not break the file."""
    if not raw or not raw.strip():
        return []
    return [part.strip() for part in raw.split("|") if part.strip()]


# ---------------------------------------------------------------------------
# LOADING
# ---------------------------------------------------------------------------

def load_masters_into_db(verbose: bool = True) -> dict[str, int]:
    """
    Rebuild the database from the CSVs.

    DROPS AND RECREATES every table on each call. This is deliberate.
    `already_invoiced_paise` is mutable state -- it grows as invoices are
    accepted -- so without a clean rebuild, running a batch twice would
    double-count and every result after the first run would be wrong.

    The CSVs are the pristine starting state. The database is disposable.
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    for path, label in [
        (config.VENDOR_MASTER_PATH, "vendor_master.csv"),
        (config.PO_MASTER_PATH, "po_master.csv"),
    ]:
        if not Path(path).exists():
            raise FileNotFoundError(f"Missing {label} at {path}")

    with _connect() as conn:
        conn.executescript(_SCHEMA)

        # --- vendors -------------------------------------------------------
        vendors = 0
        with open(config.VENDOR_MASTER_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                conn.execute(
                    "INSERT INTO vendors VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["vendor_id"].strip(),
                        row["legal_name"].strip(),
                        row["aliases"].strip(),
                        (row["gstin"] or "").strip() or None,
                        1 if _parse_bool(row["is_approved"]) else 0,
                        _parse_optional_date(row.get("onboarded_date")),
                    ),
                )
                vendors += 1

        # --- purchase orders ----------------------------------------------
        pos = 0
        with open(config.PO_MASTER_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                status = row["status"].strip().upper()
                if status not in {s.value for s in POStatus}:
                    raise ValueError(
                        f"{row['po_number']}: unknown status '{status}'"
                    )

                total_paise = rupees_to_paise(row["po_total_rupees"])
                billed_paise = rupees_to_paise(row["already_invoiced_rupees"])

                if billed_paise > total_paise:
                    raise ValueError(
                        f"{row['po_number']}: already_invoiced exceeds po_total"
                    )

                conn.execute(
                    "INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["po_number"].strip(),
                        row["vendor_id"].strip(),
                        row["vendor_name"].strip(),
                        _parse_optional_date(row["po_date"]),
                        total_paise,
                        row["currency"].strip(),
                        billed_paise,
                        status,
                        _parse_optional_int(row.get("expected_gst_rate")),
                        (row.get("description") or "").strip() or None,
                        _parse_optional_date(row.get("valid_until")),
                    ),
                )
                pos += 1

        orphans = conn.execute(
            """SELECT po_number FROM purchase_orders
               WHERE vendor_id NOT IN (SELECT vendor_id FROM vendors)"""
        ).fetchall()
        if orphans:
            raise ValueError(
                f"POs referencing unknown vendors: {[r[0] for r in orphans]}"
            )

        # --- seed invoice history (optional) --------------------------------
        # Real AP systems are onboarded mid-contract, so some invoices already
        # exist. Without this, EVERY vendor's first invoice in the batch would
        # trip R-203 -- eight spurious warnings that wreck the happy path.
        history = 0
        if Path(config.INVOICE_HISTORY_PATH).exists():
            with open(config.INVOICE_HISTORY_PATH, newline="",
                      encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    conn.execute(
                        """INSERT INTO processed_invoices
                           (vendor_id, invoice_number, po_number, subtotal_paise,
                            invoice_date, service_period_from, service_period_to,
                            decision, processed_at)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            row["vendor_id"].strip(),
                            row["invoice_number"].strip(),
                            (row.get("po_number") or "").strip() or None,
                            rupees_to_paise(row["subtotal_rupees"]),
                            _parse_optional_date(row["invoice_date"]),
                            _parse_optional_date(row.get("period_from")),
                            _parse_optional_date(row.get("period_to")),
                            "SEEDED",
                            "pre-window",
                        ),
                    )
                    history += 1

            # Seed history must reconcile with already_invoiced. If a PO says
            # Rs 5,00,000 was billed, invoices totalling that must exist.
            mismatches = conn.execute(
                """SELECT po.po_number, po.already_invoiced_paise,
                          COALESCE(SUM(pi.subtotal_paise), 0) AS seeded
                   FROM purchase_orders po
                   LEFT JOIN processed_invoices pi
                          ON pi.po_number = po.po_number
                   GROUP BY po.po_number
                   HAVING po.already_invoiced_paise != seeded"""
            ).fetchall()
            if mismatches:
                detail = ", ".join(
                    f"{r['po_number']}: PO says {r['already_invoiced_paise']} "
                    f"but history totals {r['seeded']}"
                    for r in mismatches
                )
                raise ValueError(f"Seed history does not reconcile -- {detail}")

    if verbose:
        print(f"Loaded {vendors} vendors, {pos} purchase orders, "
              f"{history} seeded invoices.")

    return {"vendors": vendors, "purchase_orders": pos, "history": history}


# ---------------------------------------------------------------------------
# ROW -> MODEL
# ---------------------------------------------------------------------------

def _row_to_po(row: sqlite3.Row) -> PurchaseOrder:
    return PurchaseOrder(
        po_number=row["po_number"],
        vendor_id=row["vendor_id"],
        vendor_name=row["vendor_name"],
        po_date=row["po_date"],
        po_total_paise=row["po_total_paise"],
        currency=row["currency"],
        already_invoiced_paise=row["already_invoiced_paise"],
        status=POStatus(row["status"]),
        expected_gst_rate=row["expected_gst_rate"],
        description=row["description"],
        valid_until=row["valid_until"],
    )


def _row_to_vendor(row: sqlite3.Row) -> Vendor:
    return Vendor(
        vendor_id=row["vendor_id"],
        legal_name=row["legal_name"],
        aliases=_split_aliases(row["aliases"]),
        gstin=row["gstin"],
        is_approved=bool(row["is_approved"]),
        onboarded_date=row["onboarded_date"],
    )


# ---------------------------------------------------------------------------
# QUERIES
# ---------------------------------------------------------------------------

def get_po(po_number: str) -> PurchaseOrder | None:
    """Fetch one PO. Returns None if it does not exist."""
    _ensure_loaded()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM purchase_orders WHERE po_number = ?",
            (po_number.strip().upper(),),
        ).fetchone()
    return _row_to_po(row) if row else None


def get_vendor(vendor_id: str) -> Vendor | None:
    _ensure_loaded()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM vendors WHERE vendor_id = ?", (vendor_id.strip(),)
        ).fetchone()
    return _row_to_vendor(row) if row else None


def get_all_vendors() -> list[Vendor]:
    """Every vendor. Matching Layer 2 scans these for a fuzzy name match."""
    _ensure_loaded()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM vendors ORDER BY vendor_id").fetchall()
    return [_row_to_vendor(r) for r in rows]


def find_pos_by_vendor(
    vendor_id: str, open_only: bool = True
) -> list[PurchaseOrder]:
    """All POs for a vendor. Used by matching Layer 2."""
    _ensure_loaded()
    sql = "SELECT * FROM purchase_orders WHERE vendor_id = ?"
    params: list = [vendor_id.strip()]
    if open_only:
        sql += " AND status = ?"
        params.append(POStatus.OPEN.value)
    sql += " ORDER BY po_date"

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_po(r) for r in rows]


def get_all_pos() -> list[PurchaseOrder]:
    _ensure_loaded()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM purchase_orders ORDER BY po_number"
        ).fetchall()
    return [_row_to_po(r) for r in rows]


# ---------------------------------------------------------------------------
# STATE MUTATION
# ---------------------------------------------------------------------------

def update_already_invoiced(po_number: str, add_paise: int) -> PurchaseOrder:
    """
    Add an accepted invoice's subtotal to a PO's running total.

    Called ONLY for accepted invoices -- AUTO_APPROVE and APPROVE_WITH_FLAG.
    HOLD_FOR_REVIEW and REJECT must not touch this, or a held invoice would
    consume budget it was never approved for.

    This is the state that makes progressive billing (EC-1) work: it turns a
    PO from a static number into a running balance. Single-user only, no row
    locking. See Spec A-11.
    """
    _ensure_loaded()

    if add_paise < 0:
        raise ValueError("Cannot add a negative amount to already_invoiced")

    key = po_number.strip().upper()

    with _connect() as conn:
        row = conn.execute(
            "SELECT po_number FROM purchase_orders WHERE po_number = ?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(f"PO not found: {po_number}")

        conn.execute(
            """UPDATE purchase_orders
               SET already_invoiced_paise = already_invoiced_paise + ?
               WHERE po_number = ?""",
            (add_paise, key),
        )
        updated = conn.execute(
            "SELECT * FROM purchase_orders WHERE po_number = ?", (key,)
        ).fetchone()

    return _row_to_po(updated)


def record_processed_invoice(
    vendor_id: str,
    invoice_number: str | None,
    subtotal_paise: int,
    invoice_date: str,
    po_number: str | None = None,
    service_period: dict | None = None,
    decision: str | None = None,
) -> None:
    """
    History for duplicate detection (R-501, R-502).

    Recorded for EVERY processed invoice regardless of decision -- a rejected
    duplicate still needs to be on record, or the same invoice submitted a
    third time would look brand new.
    """
    _ensure_loaded()

    period_from = period_to = None
    if service_period:
        period_from = service_period.get("from_date") or service_period.get("from")
        period_to = service_period.get("to_date") or service_period.get("to")
        period_from = str(period_from) if period_from else None
        period_to = str(period_to) if period_to else None

    with _connect() as conn:
        conn.execute(
            """INSERT INTO processed_invoices
               (vendor_id, invoice_number, po_number, subtotal_paise,
                invoice_date, service_period_from, service_period_to,
                decision, processed_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                vendor_id,
                invoice_number,
                po_number,
                subtotal_paise,
                invoice_date,
                period_from,
                period_to,
                decision,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def find_prior_invoices(vendor_id: str) -> list[dict]:
    """Past invoices from this vendor, newest first. Feeds the duplicate rules."""
    _ensure_loaded()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM processed_invoices
               WHERE vendor_id = ? ORDER BY invoice_date DESC""",
            (vendor_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_prior_invoices(vendor_id: str) -> int:
    """Used by R-203 (first invoice from this vendor)."""
    _ensure_loaded()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM processed_invoices WHERE vendor_id = ?",
            (vendor_id,),
        ).fetchone()
    return int(row["n"])
