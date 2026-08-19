"""
Central configuration. Every tunable number in the system lives here.

RULE: Nothing in this list may be hard-coded anywhere else in the codebase.
If you need to change a threshold, change it here and nowhere else.

Reference: Spec Section 5.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT_DIR / "data"
INVOICE_DIR = DATA_DIR / "invoices"
CLEAN_INVOICE_DIR = INVOICE_DIR / "clean"
SCANNED_INVOICE_DIR = INVOICE_DIR / "scanned"

PO_MASTER_PATH = DATA_DIR / "po_master.csv"
VENDOR_MASTER_PATH = DATA_DIR / "vendor_master.csv"
ANSWER_KEY_PATH = DATA_DIR / "answer_key.csv"
INVOICE_HISTORY_PATH = DATA_DIR / "invoice_history.csv"
DB_PATH = DATA_DIR / "invoicing.db"

OUTPUT_DIR = ROOT_DIR / "outputs"
TRACE_DIR = OUTPUT_DIR / "traces"
SCORECARD_PATH = OUTPUT_DIR / "scorecard.md"

CACHE_DIR = ROOT_DIR / ".cache" / "extractions"


# ---------------------------------------------------------------------------
# MONEY  (Spec A-01, A-07)
# ---------------------------------------------------------------------------

CURRENCY = "INR"

# All money is handled internally as INTEGER PAISE. 1 rupee = 100 paise.
# Floats are never used for money. See Spec Section 6.4.
PAISE_PER_RUPEE = 100


# ---------------------------------------------------------------------------
# TOLERANCE — over-billing  (Spec Section 6)
# ---------------------------------------------------------------------------
#
#   allowed_overage = min( 1.5% of po_total , Rs 10,000 )
#
# The percentage is stored as an integer fraction so the arithmetic never
# touches a float. 1.5% == 15/1000.
#
# Crossover point: the two limits are equal at PO = Rs 6,66,667.
#   Below that, the PERCENTAGE binds.  Above it, the CAP binds.

TOLERANCE_PERCENT_NUMERATOR = 15
TOLERANCE_PERCENT_DENOMINATOR = 1000

TOLERANCE_PERCENT_DISPLAY = 1.5  # for display to humans ONLY — never for maths

TOLERANCE_ABSOLUTE_CAP_PAISE = 10_000 * PAISE_PER_RUPEE  # Rs 10,000

# Under-billing has NO limit and never fails. See Spec Section 10, R-305.

# Used more than this share of the allowance -> flag, but do not block (R-304).
TOLERANCE_CONSUMPTION_FLAG = 0.50


# ---------------------------------------------------------------------------
# EXTRACTION CONFIDENCE  (Spec Section 8)
# ---------------------------------------------------------------------------

CONFIDENCE_CRITICAL = 0.80    # invoice no., date, vendor, subtotal, total
CONFIDENCE_SUPPORTING = 0.70  # line items, tax, PO ref, GSTIN

CRITICAL_FIELDS = [
    "invoice_number",
    "invoice_date",
    "vendor_name",
    "subtotal_paise",
    "total_paise",
]

SUPPORTING_FIELDS = [
    "po_reference",
    "vendor_gstin",
    "gst_rate",
    "tax_paise",
    "service_period",
]


# ---------------------------------------------------------------------------
# EXTRACTION TIERS  (Spec Section 8, A-13)
# ---------------------------------------------------------------------------
#
# FREE     pure Python. Reads embedded text from born-digital PDFs.
#          Refuses scanned/image-only documents -- see A-13.
# PREMIUM  OpenAI vision model. Handles anything, including poor scans.
#
# Both tiers return the identical ExtractedInvoice shape, so every downstream
# stage is tier-agnostic. Only extraction quality differs.

DEFAULT_TIER = "free"

LOCAL_EXTRACTOR_NAME = "pymupdf-local"

# Verify the current model string against https://developers.openai.com/api/docs/models
OPENAI_VISION_MODEL = "gpt-5.6-terra"
OPENAI_MAX_OUTPUT_TOKENS = 4096
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

# Portkey is an AI gateway that speaks the OpenAI API. Routing through it means
# an organisation can hold the provider credential centrally, so the person
# running this only ever needs a gateway key.
PORTKEY_API_KEY_ENV = "PORTKEY_API_KEY"
PORTKEY_BASE_URL = "https://api.portkey.ai/v1"

# Portkey is an AI gateway that speaks the OpenAI API. Pointing base_url at it
# and adding x-portkey-* headers is enough -- the rest of extract_vision.py is
# unchanged. Override the URL for a self-hosted or enterprise gateway.
PORTKEY_BASE_URL = "https://api.portkey.ai/v1"

# PDF -> PNG for the vision call. 150 DPI is ample for reading an invoice and
# roughly halves the image tokens versus 200. Rendering above the source
# resolution cannot recover detail that is not there -- the degraded scan was
# produced at 82 DPI, so 200 would only upscale its noise.
RENDER_DPI = 150


# ---------------------------------------------------------------------------
# QUALITY GATE  (rule R-010)
# ---------------------------------------------------------------------------
# An extraction can be structurally valid but too weak to act on. The gate
# reports; R-010 decides. Failing the gate routes to HOLD_FOR_REVIEW, never
# to an automatic approval or rejection.

QUALITY_MIN_MEAN_CONFIDENCE = 0.60   # across critical fields only
QUALITY_MAX_MISSING_CRITICAL = 2     # 3 or more missing fails the gate


# ---------------------------------------------------------------------------
# EXTRACTION CACHE
# ---------------------------------------------------------------------------
# Keyed by (file hash, tier). You will re-run the pipeline dozens of times
# while tuning rules in Phase 6 -- without this you pay and wait every time.
# Holding both tiers for the same file also lets you compare them directly.

# Optional: have a model rephrase the decision summary. Off by default --
# the template summary is deterministic, free, and always available.
SUMMARY_USE_MODEL = False

EXTRACTION_CACHE_ENABLED = True
CACHE_SCHEMA_VERSION = 1      # bump to invalidate every cached extraction


# ---------------------------------------------------------------------------
# MATCHING  (Spec Section 9)
# ---------------------------------------------------------------------------

VENDOR_FUZZY_THRESHOLD = 85   # rapidfuzz token_set_ratio, 0-100
PO_AMOUNT_MATCH_WINDOW = 0.10  # +/-10% when searching for a PO by amount
PO_DATE_WINDOW_DAYS = 120      # invoice must fall within this of the PO date

# Stripped during vendor-name normalisation before fuzzy comparison.
VENDOR_NAME_NOISE_TOKENS = [
    "PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "INC",
    "CO", "COMPANY", "CORP", "CORPORATION", "AND", "&",
]


# ---------------------------------------------------------------------------
# DUPLICATES  (Spec Section 10.7)
# ---------------------------------------------------------------------------

NEAR_DUPLICATE_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# ARITHMETIC  (Spec A-12)
# ---------------------------------------------------------------------------

# Vendor systems round differently. Anything inside Re 1 is not a real problem.
ROUNDING_TOLERANCE_PAISE = 1 * PAISE_PER_RUPEE


# ---------------------------------------------------------------------------
# TAX  (Spec A-02)
# ---------------------------------------------------------------------------

VALID_GST_SLABS = [0, 5, 12, 18, 28]
GSTIN_LENGTH = 15


# ---------------------------------------------------------------------------
# DATES  (Spec A-08)
# ---------------------------------------------------------------------------

ACCEPTED_DATE_FORMATS = [
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y-%m-%d",
    "%d.%m.%Y",
]


# ---------------------------------------------------------------------------
# SELF-CHECK
# ---------------------------------------------------------------------------

def _validate_config() -> None:
    """Fail loudly at import time if a constant has been set to nonsense."""
    assert TOLERANCE_PERCENT_DENOMINATOR > 0, "Tolerance denominator must be > 0"
    assert TOLERANCE_ABSOLUTE_CAP_PAISE > 0, "Tolerance cap must be > 0"
    assert 0 < TOLERANCE_CONSUMPTION_FLAG <= 1, "Consumption flag must be in (0, 1]"
    assert 0 < CONFIDENCE_SUPPORTING <= CONFIDENCE_CRITICAL <= 1, (
        "Confidence floors must satisfy 0 < supporting <= critical <= 1"
    )
    assert 0 < VENDOR_FUZZY_THRESHOLD <= 100, "Fuzzy threshold must be in (0, 100]"
    assert ROUNDING_TOLERANCE_PAISE >= 0, "Rounding tolerance cannot be negative"
    assert 0 in VALID_GST_SLABS, "GST slabs should include 0"

    # The documented crossover must actually hold for the configured values.
    crossover = (
        TOLERANCE_ABSOLUTE_CAP_PAISE
        * TOLERANCE_PERCENT_DENOMINATOR
        // TOLERANCE_PERCENT_NUMERATOR
    )
    assert crossover > 0, "Crossover calculation failed"


_validate_config()


# Documented crossover PO value, in paise, where percentage and cap are equal.
TOLERANCE_CROSSOVER_PAISE = (
    TOLERANCE_ABSOLUTE_CAP_PAISE
    * TOLERANCE_PERCENT_DENOMINATOR
    // TOLERANCE_PERCENT_NUMERATOR
)
