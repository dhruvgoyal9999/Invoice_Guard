"""
Invoice decisioning interface.

    streamlit run app/streamlit_app.py

Design brief: a modern finance-SaaS look -- sidebar navigation, card surfaces,
icons throughout, emerald accent -- built so a non-technical reviewer can watch
an invoice being assessed and understand what happened and why.

Three principles carried through the redesign:

  NARRATE, DO NOT JUST REPORT.  The five pipeline stages announce what they did
  in plain English as they happen. Those strings come from pipeline.py -- this
  file shows them, it does not compose them.

  PROGRESSIVE DISCLOSURE.  Decision first, then the story, then the evidence,
  then the raw record. Nobody is forced through the technical layer to reach
  the answer.

  NO LOGIC LIVES HERE.  No thresholds, no rules, no decision policy. Every
  number shown came out of a trace produced without this file.

Theme follows the user's system. Colours are built from rgba overlays and a
prefers-color-scheme swap so the same CSS reads correctly on light and dark.
"""

import base64
import sys
import time
from datetime import date
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, credentials as cred, store             # noqa: E402
from src.money import allowed_overage, format_paise            # noqa: E402
from src.pipeline import process_invoice                       # noqa: E402
from src.rules import rule_catalogue                           # noqa: E402
from src.schemas import Decision, RuleStatus, Severity, Tier    # noqa: E402
from src.trace import write_trace                              # noqa: E402

BATCH_DATE = date(2026, 8, 10)

# A short pause between stages. The free reader finishes in ~50ms, so without
# it the narration flashes past unread. A comprehension aid, not a simulation
# of work -- the work really is that fast.
STAGE_PAUSE = 0.45

st.set_page_config(page_title="Invoice Decisioning",
                   page_icon=":material/receipt_long:",
                   layout="wide", initial_sidebar_state="expanded")


# ---------------------------------------------------------------------------
# LANGUAGE
# ---------------------------------------------------------------------------

DECISION_UI = {
    Decision.AUTO_APPROVE: {
        "label": "Approved automatically",
        "icon": "task_alt", "var": "--ok",
        "what": "Everything checked out. This goes straight to payment with no one needing to look at it.",
    },
    Decision.APPROVE_WITH_FLAG: {
        "label": "Approved, with a note",
        "icon": "flag", "var": "--flag",
        "what": "The money side is fine, so it will be paid. Something is worth recording for the audit trail.",
    },
    Decision.HOLD_FOR_REVIEW: {
        "label": "Held for a person to review",
        "icon": "pause_circle", "var": "--hold",
        "what": "Something is wrong or unclear. It might still be perfectly legitimate, but the system cannot tell on its own.",
    },
    Decision.REJECT: {
        "label": "Rejected",
        "icon": "block", "var": "--bad",
        "what": "This cannot be paid as submitted, no matter the amount. Something fundamental is wrong.",
    },
}

SEVERITY_MEANING = {
    Severity.BLOCKER: "Stops payment outright",
    Severity.CRITICAL: "Needs a person to look",
    Severity.WARNING: "Worth noting, does not stop payment",
    Severity.INFO: "Recorded for the record only",
}

STATUS_LABEL = {
    RuleStatus.PASS: "Passed", RuleStatus.FAIL: "Failed",
    RuleStatus.SKIP: "Could not check", RuleStatus.WARN: "Warning",
}

STAGES = {
    1: ("Reading the invoice", "document_scanner",
        "Pulling the numbers off the page. Nothing is decided here - the reader reports what it can see, and says so when it cannot read something."),
    2: ("Finding the purchase order", "manage_search",
        "Every invoice should relate to something the company already agreed to buy. This finds that agreement."),
    3: ("Comparing the amounts", "calculate",
        "Checking what is being billed against what is still owed on that agreement."),
    4: ("Running the checks", "checklist",
        "Thirty-three independent checks, all run every time. None can stop the others from running."),
    5: ("Reaching a decision", "gavel",
        "The checks do not decide anything themselves. The most serious failure determines the outcome."),
}


# ---------------------------------------------------------------------------
# STYLE
# ---------------------------------------------------------------------------

CSS = """
<style>
  /* Material Symbols. Streamlit renders :material/x: in its OWN markdown, but
     NOT inside unsafe_allow_html blocks -- there it stays literal text. So for
     any HTML we build ourselves we use the font directly via .mi. */
  @import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200');

  .mi {
    font-family: 'Material Symbols Rounded', 'Material Symbols Outlined';
    font-weight: normal; font-style: normal; font-size: 1.1em;
    line-height: 1; letter-spacing: normal; text-transform: none !important;
    display: inline-block; white-space: nowrap; word-wrap: normal;
    direction: ltr; vertical-align: -0.18em;
    -webkit-font-feature-settings: 'liga'; font-feature-settings: 'liga';
    -webkit-font-smoothing: antialiased;
  }

  :root {
    --accent:      #059669;
    --accent-soft: rgba(5,150,105,0.10);
    --ok:   #059669;  --ok-soft:   rgba(5,150,105,0.10);
    --flag: #b45309;  --flag-soft: rgba(180,83,9,0.10);
    --hold: #c2620d;  --hold-soft: rgba(194,98,13,0.10);
    --bad:  #b91c1c;  --bad-soft:  rgba(185,28,28,0.10);

    --page:   #f2f6f4;
    --card:   #ffffff;
    --line:   rgba(16,40,32,0.11);
    --shadow: 0 1px 2px rgba(16,40,32,.05), 0 2px 8px rgba(16,40,32,.05);
    --muted:  rgba(60,70,66,0.62);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --accent: #34d399; --accent-soft: rgba(52,211,153,0.14);
      --ok:   #34d399;  --ok-soft:   rgba(52,211,153,0.14);
      --flag: #fbbf24;  --flag-soft: rgba(251,191,36,0.14);
      --hold: #fb923c;  --hold-soft: rgba(251,146,60,0.14);
      --bad:  #f87171;  --bad-soft:  rgba(248,113,113,0.14);

      --page:   #0d1512;
      --card:   rgba(255,255,255,0.038);
      --line:   rgba(255,255,255,0.12);
      --shadow: none;
      --muted:  rgba(230,240,236,0.60);
    }
  }

  /* ---- page ---- */
  /* Two soft radial washes over the base colour. Fixed attachment so they stay
     put while the content scrolls, which reads as a backdrop rather than a
     decoration stuck to the top of the page. */
  .stApp {
    background:
      radial-gradient(1100px 520px at 12% -12%, rgba(5,150,105,0.13), transparent 62%),
      radial-gradient(900px 420px at 92% 2%,  rgba(13,148,136,0.10), transparent 58%),
      var(--page);
    background-attachment: fixed;
  }
  @media (prefers-color-scheme: dark) {
    .stApp {
      background:
        radial-gradient(1100px 520px at 12% -12%, rgba(52,211,153,0.10), transparent 62%),
        radial-gradient(900px 420px at 92% 2%,  rgba(45,212,191,0.07), transparent 58%),
        var(--page);
      background-attachment: fixed;
    }
  }
  section[data-testid="stSidebar"] {
    background: var(--card); border-right: 1px solid var(--line);
  }
  .block-container {padding-top: 2.1rem; padding-bottom: 4rem; max-width: 1280px;}
  h1,h2,h3,h4,h5 {letter-spacing: -0.018em;}

  /* ---- equal-height columns ---- */
  div[data-testid="stHorizontalBlock"] {align-items: stretch;}
  div[data-testid="stColumn"] > div {height: 100%;}
  div[data-testid="stColumn"] div[data-testid="stVerticalBlock"] {height: 100%;}

  /* ---- buttons ---- */
  .stButton button[kind="primary"],
  .stDownloadButton button[kind="primary"],
  button[data-testid="stBaseButton-primary"] {
    background: var(--accent) !important; border-color: var(--accent) !important;
    color: #fff !important; font-weight: 600;
  }
  .stButton button[kind="primary"]:hover,
  button[data-testid="stBaseButton-primary"]:hover {filter: brightness(1.07);}
  .stButton button, .stDownloadButton button {border-radius: 9px;}

  /* ---- sidebar nav ---- */
  section[data-testid="stSidebar"] .stButton button {
    width: 100%; justify-content: flex-start; text-align: left;
    border: 1px solid transparent; background: transparent;
    padding: 0.52rem 0.75rem; font-weight: 500; color: inherit;
  }
  section[data-testid="stSidebar"] .stButton button:hover {
    background: var(--accent-soft); border-color: var(--line);
  }
  .navactive + div button, .navactive button {
    background: var(--accent-soft) !important;
    border-color: var(--accent) !important;
    color: var(--accent) !important; font-weight: 650 !important;
  }
  .brand {
    display: flex; align-items: center; gap: 11px;
    font-size: 1.02rem; font-weight: 700; padding: 4px 0 16px 0; line-height: 1.2;
  }
  .brand-dot {
    width: 32px; height: 32px; border-radius: 10px; flex: 0 0 32px;
    background: var(--accent-soft); color: var(--accent);
    display: flex; align-items: center; justify-content: center; font-size: 18px;
  }

  /* ---- cards ---- */
  .card {
    border: 1px solid var(--line); border-radius: 14px; background: var(--card);
    box-shadow: var(--shadow); padding: 18px 20px;
    height: 100%; display: flex; flex-direction: column;
  }
  .stat-k {
    font-size: .705rem; text-transform: uppercase; letter-spacing: .075em;
    color: var(--muted); font-weight: 650;
    display: flex; align-items: center; gap: 7px;
  }
  .stat-v {font-size: 1.42rem; font-weight: 700; margin-top: 7px; line-height: 1.2;}
  .stat-n {font-size: .79rem; color: var(--muted); margin-top: 5px;}

  /* ---- hero ---- */
  .hero {
    border: 1px solid var(--line); border-radius: 16px; padding: 24px 28px;
    display: flex; gap: 18px; align-items: flex-start; box-shadow: var(--shadow);
  }
  .hero-ico {
    width: 50px; height: 50px; border-radius: 14px; flex: 0 0 50px;
    display: flex; align-items: center; justify-content: center; font-size: 26px;
  }
  .hero-t {font-size: 1.5rem; font-weight: 700; line-height: 1.2;}
  .hero-w {font-size: .95rem; opacity: .85; margin-top: 6px; max-width: 66ch;}
  .hero-m {
    margin-top: 14px; padding-top: 11px; border-top: 1px solid var(--line);
    font-size: .82rem; color: var(--muted);
  }

  /* ---- pills ---- */
  .pill {
    display: inline-flex; align-items: center; padding: 2px 10px;
    border-radius: 999px; font-size: .715rem; font-weight: 650;
    border: 1px solid; margin-left: 4px;
  }

  /* ---- stepper ---- */
  .stepper {
    border: 1px solid var(--line); border-radius: 14px; overflow: hidden;
    background: var(--card); box-shadow: var(--shadow);
  }
  .step {display: flex; gap: 14px; padding: 14px 18px;
         border-bottom: 1px solid var(--line);}
  .step:last-child {border-bottom: none;}
  .step-ico {
    flex: 0 0 32px; height: 32px; border-radius: 10px; font-size: 18px;
    display: flex; align-items: center; justify-content: center;
  }
  .s-done {background: var(--ok-soft); color: var(--ok);}
  .s-warn {background: var(--hold-soft); color: var(--hold);}
  .s-idle {background: rgba(140,140,140,.11); color: var(--muted);}
  .s-live {background: var(--accent-soft); color: var(--accent);
           animation: pulse 1.1s ease-in-out infinite;}
  @keyframes pulse {0%,100%{opacity:.5} 50%{opacity:1}}
  .step-t {font-weight: 640; font-size: .95rem;}
  .step-d {font-size: .875rem; opacity: .85; margin-top: 3px; max-width: 84ch;}
  .step-w {font-size: .79rem; color: var(--muted); margin-top: 5px;
           font-style: italic;}

  /* ---- ring ---- */
  .ring-wrap {display: flex; align-items: center; gap: 16px;}
  .ring {width: 84px; height: 84px; transform: rotate(-90deg); flex: 0 0 84px;}
  .ring-bg {fill: none; stroke: var(--line); stroke-width: 10;}
  .ring-fg {
    fill: none; stroke: var(--accent); stroke-width: 10; stroke-linecap: round;
    stroke-dasharray: 264;
    animation: fill 900ms cubic-bezier(.22,1,.36,1) forwards;
  }
  @keyframes fill {from {stroke-dashoffset: 264;}}
  .ring-lbl {font-size: 1.3rem; font-weight: 700;}
  .ring-sub {font-size: .8rem; color: var(--muted);}

  /* ---- bars ---- */
  .bar {height: 7px; border-radius: 999px; background: rgba(140,140,140,.16);
        overflow: hidden; margin-top: 9px;}
  .bar-f {height: 100%; border-radius: 999px;
          animation: grow 800ms cubic-bezier(.22,1,.36,1) forwards;}
  @keyframes grow {from {width: 0 !important;}}

  /* ---- rule cards ---- */
  .rc {
    border: 1px solid var(--line); border-left-width: 3px; border-radius: 11px;
    padding: 13px 16px; margin-bottom: 10px; background: var(--card);
    box-shadow: var(--shadow);
  }
  .rc-h {font-weight: 640; font-size: .94rem; display: flex;
         align-items: center; gap: 8px; flex-wrap: wrap;}
  .rc-id {font-family: ui-monospace, monospace; font-size: .75rem; opacity: .5;}
  .rc-m {font-size: .88rem; opacity: .88; margin-top: 5px;}
  .rc-c {font-size: .78rem; color: var(--muted); margin-top: 6px;
         font-family: ui-monospace, monospace;}

  .empty {
    border: 1px dashed var(--line); border-radius: 16px; padding: 42px 30px;
    text-align: center; background: var(--card);
  }

  /* ---- tables ---- */
  div[data-testid="stDataFrame"] {border-radius: 12px; overflow: hidden;}

  /* ---- landing hero band ---- */
  .band {
    border: 1px solid var(--line); border-radius: 20px; padding: 38px 40px 34px;
    background:
      linear-gradient(135deg, var(--accent-soft) 0%, transparent 55%),
      var(--card);
    box-shadow: var(--shadow); position: relative; overflow: hidden;
  }
  .band::after {
    content: ""; position: absolute; right: -70px; top: -70px;
    width: 260px; height: 260px; border-radius: 50%;
    background: var(--accent-soft); opacity: .55;
  }
  .band > * {position: relative; z-index: 1;}
  .eyebrow {
    display: inline-flex; align-items: center; gap: 7px; padding: 5px 13px;
    border-radius: 999px; background: var(--accent-soft); color: var(--accent);
    font-size: .74rem; font-weight: 700; letter-spacing: .05em;
    text-transform: uppercase; border: 1px solid var(--accent);
  }
  .band-h {
    font-size: 2.5rem; font-weight: 800; line-height: 1.1; margin: 16px 0 0;
    letter-spacing: -0.03em; max-width: 20ch;
  }
  .band-h em {font-style: normal; color: var(--accent);}
  .band-p {font-size: 1.02rem; opacity: .82; margin-top: 14px; max-width: 62ch;
           line-height: 1.6;}

  /* ---- icon tile inside a stat card ---- */
  .tile {
    width: 34px; height: 34px; border-radius: 10px; flex: 0 0 34px;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px; margin-bottom: 12px;
  }

  /* ---- callout ---- */
  .callout {
    border: 1px solid var(--accent); border-left-width: 4px; border-radius: 14px;
    background: var(--accent-soft); padding: 20px 24px;
  }
  .callout-h {font-weight: 750; font-size: 1.05rem; color: var(--accent);
              display: flex; align-items: center; gap: 9px;}
  .callout-b {margin-top: 9px; opacity: .88; line-height: 1.62; max-width: 82ch;}

  /* ---- five-step flow ---- */
  .flow {display: flex; gap: 0; align-items: stretch; flex-wrap: wrap;}
  .flow-i {
    flex: 1 1 0; min-width: 168px; padding: 18px 16px; position: relative;
    border: 1px solid var(--line); background: var(--card);
    box-shadow: var(--shadow);
  }
  .flow-i:first-child {border-radius: 14px 0 0 14px;}
  .flow-i:last-child  {border-radius: 0 14px 14px 0;}
  .flow-i + .flow-i {border-left: none;}
  .flow-n {
    font-size: .68rem; font-weight: 800; letter-spacing: .1em;
    color: var(--accent); text-transform: uppercase;
  }
  .flow-t {font-weight: 650; font-size: .93rem; margin-top: 8px;
           display: flex; align-items: center; gap: 8px;}
  .flow-d {font-size: .81rem; color: var(--muted); margin-top: 6px;
           line-height: 1.5;}

  /* ---- outcome mini cards ---- */
  .oc {
    border: 1px solid var(--line); border-top-width: 3px; border-radius: 12px;
    padding: 15px 17px; background: var(--card); box-shadow: var(--shadow);
    height: 100%;
  }
  .oc-t {font-weight: 700; font-size: .93rem; display: flex;
         align-items: center; gap: 8px;}
  .oc-d {font-size: .81rem; color: var(--muted); margin-top: 7px; line-height: 1.5;}
</style>
"""


def ico(name: str) -> str:
    """Icon for HTML we build ourselves. Uses the Material Symbols font, because
    :material/x: is only interpreted by Streamlit's own markdown renderer."""
    return f'<span class="mi">{name}</span>'


def mi(name: str) -> str:
    """Icon for Streamlit-rendered markdown, captions, labels and buttons."""
    return f":material/{name}:"


def card_stat(col, icon: str, key: str, value: str, note: str = "",
              var: str = "--accent") -> None:
    col.markdown(
        f'<div class="card">'
        f'<div class="tile" style="background:var({var}-soft);color:var({var});">'
        f'{ico(icon)}</div>'
        f'<div class="stat-k"><span>{key}</span></div>'
        f'<div class="stat-v">{value}</div><div class="stat-n">{note}</div></div>',
        unsafe_allow_html=True)


def pill(text: str, var: str) -> str:
    return (f'<span class="pill" style="color:var({var});border-color:var({var});'
            f'background:var({var}-soft);">{text}</span>')


def ring(passed: int, total: int, label: str) -> str:
    pct = passed / total if total else 0
    offset = 264 * (1 - pct)
    return (
        f'<div class="ring-wrap">'
        f'<svg class="ring" viewBox="0 0 100 100">'
        f'<circle class="ring-bg" cx="50" cy="50" r="42"/>'
        f'<circle class="ring-fg" cx="50" cy="50" r="42" '
        f'style="stroke-dashoffset:{offset:.1f};"/></svg>'
        f'<div><div class="ring-lbl">{passed} / {total}</div>'
        f'<div class="ring-sub">{label}</div></div></div>')


def bar(pct: float, var: str) -> str:
    return (f'<div class="bar"><div class="bar-f" style="width:{min(pct,100):.0f}%;'
            f'background:var({var});"></div></div>')


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------

NAV = [("overview", "Overview", "dashboard"),
       ("assess", "Assess an invoice", "bolt"),
       ("orders", "Purchase orders", "receipt_long"),
       ("queue", "Everything assessed", "list_alt"),
       ("learn", "How this works", "menu_book")]


def init_state() -> None:
    if "traces" not in st.session_state:
        st.session_state.traces, st.session_state.order = {}, []
    if "nav" not in st.session_state:
        st.session_state.nav = "overview"
    # Plain keys, deliberately not widget keys -- see the note by the reader
    # radio. Anything that must survive a navigation lives here.
    if "tier_choice" not in st.session_state:
        st.session_state.tier_choice = "Free"
    if "creds" not in st.session_state:
        st.session_state.creds = {}
    if "db_ready" not in st.session_state:
        store.load_masters_into_db(verbose=False)
        st.session_state.db_ready = True


def corpus() -> list[Path]:
    files = list(config.CLEAN_INVOICE_DIR.glob("*.pdf")) + \
            list(config.SCANNED_INVOICE_DIR.glob("*.pdf"))
    return sorted(files, key=lambda p: p.stem)


def reset_everything() -> None:
    store.load_masters_into_db(verbose=False)
    st.session_state.traces, st.session_state.order = {}, []


def remember(path: Path, trace) -> None:
    stem = Path(path).stem
    if stem not in st.session_state.traces:
        st.session_state.order.append(stem)
    st.session_state.traces[stem] = trace
    write_trace(trace)


@st.cache_data(show_spinner=False)
def preview_png(path_str: str, mtime: float) -> bytes:
    """First page as a PNG. mtime is in the cache key so edits invalidate it."""
    import pymupdf
    doc = pymupdf.open(path_str)
    try:
        return doc[0].get_pixmap(dpi=95).tobytes("png")
    finally:
        doc.close()


def preview_of(path: Path) -> bytes | None:
    try:
        return preview_png(str(path), Path(path).stat().st_mtime)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# STEPPER
# ---------------------------------------------------------------------------

def render_steps(done: dict[int, str], current: int | None) -> str:
    rows = []
    for n, (title, icon, why) in STAGES.items():
        if n in done:
            detail = done[n]
            trouble = any(w in detail.lower() for w in
                          ("could not", "no purchase order", "failed", "over the limit"))
            cls = "s-warn" if trouble else "s-done"
            mark = ico("priority_high") if trouble else ico("check")
            body = (f'<div class="step-d">{detail}</div>'
                    f'<div class="step-w">{why}</div>')
        elif n == current:
            cls, mark = "s-live", ico(icon)
            body = '<div class="step-d">working&hellip;</div>'
        else:
            cls, mark = "s-idle", ico(icon)
            body = ""
        rows.append(f'<div class="step"><div class="step-ico {cls}">{mark}</div>'
                    f'<div><div class="step-t">{title}</div>{body}</div></div>')
    return '<div class="stepper">' + "".join(rows) + "</div>"


def run_with_narration(path: Path, tier: Tier, slot):
    done: dict[int, str] = {}

    def on_stage(step: int, title: str, detail: str) -> None:
        done[step] = detail
        slot.markdown(render_steps(done, step + 1 if step < 5 else None),
                      unsafe_allow_html=True)
        time.sleep(STAGE_PAUSE)

    slot.markdown(render_steps({}, 1), unsafe_allow_html=True)
    trace = process_invoice(path, tier, today=BATCH_DATE, on_stage=on_stage)
    slot.markdown(render_steps(done, None), unsafe_allow_html=True)
    return trace


# ---------------------------------------------------------------------------
# RESULT PANELS
# ---------------------------------------------------------------------------

def hero(trace) -> None:
    d = trace.stage_5_decision
    ui = DECISION_UI[d.decision]
    reasons = ", ".join(d.determined_by) if d.determined_by else "no check failed"
    bits = [f"{d.rules_passed} of {d.rules_run} checks passed"]
    if d.rules_failed:
        bits.append(f"{d.rules_failed} failed")
    if d.rules_skipped:
        bits.append(f"{d.rules_skipped} could not be checked")
    bits.append(f"decided by {reasons}")
    st.markdown(
        f'<div class="hero" style="background:var({ui["var"]}-soft);'
        f'border-color:var({ui["var"]});">'
        f'<div class="hero-ico" style="background:var({ui["var"]}-soft);'
        f'color:var({ui["var"]});">{ico(ui["icon"])}</div><div style="flex:1;">'
        f'<div class="hero-t" style="color:var({ui["var"]});">{ui["label"]}</div>'
        f'<div class="hero-w">{ui["what"]}</div>'
        f'<div class="hero-m">{" &middot; ".join(bits)}</div></div></div>',
        unsafe_allow_html=True)


def key_facts(trace) -> None:
    f = trace.stage_1_extraction.fields
    m = trace.stage_2_matching
    fin = trace.stage_3_financials
    c1, c2, c3, c4 = st.columns(4)
    card_stat(c1, "storefront", "Supplier", f.vendor_name.value or "not read",
              f"Invoice {f.invoice_number.value or 'number not readable'}")
    card_stat(c2, "receipt_long", "Purchase order", m.po_number or "none matched",
              "printed on the invoice" if m.match_layer == 1
              else "worked out by the system")
    card_stat(c3, "payments", "Amount billed",
              format_paise(f.subtotal_paise.value) if f.subtotal_paise.is_present else "not read",
              f"plus {format_paise(f.tax_paise.value)} tax" if f.tax_paise.is_present else "before tax")
    if fin:
        note = ("within budget" if fin.is_under_billing else
                f"{format_paise(fin.overage_paise)} over, "
                f"{'allowed' if not fin.is_breach else 'too much'}")
        card_stat(c4, "savings", "Still available",
                  format_paise(fin.remaining_balance_paise), note)
    else:
        card_stat(c4, "savings", "Still available", "n/a",
                  "no purchase order to compare against")


def why_panel(trace) -> None:
    d = trace.stage_5_decision
    c1, c2 = st.columns([1, 2])
    with c1:
        st.markdown(ring(d.rules_passed, d.rules_run, "checks passed"),
                    unsafe_allow_html=True)
    with c2:
        if not d.determined_by:
            st.markdown(f"##### {mi('verified')} Nothing needed a person")
            st.caption("Every one of the 33 checks passed.")
        else:
            st.markdown(f"##### {mi('help')} Why this outcome")
            determining = {r.rule_id: r for r in trace.stage_4_rules}
            var = DECISION_UI[d.decision]["var"]
            for rid in d.determined_by:
                r = determining[rid]
                cmp_line = ""
                if r.expected is not None and r.actual is not None:
                    cmp_line = (f'<div class="rc-c">expected {r.expected} '
                                f'&nbsp;&middot;&nbsp; found {r.actual}</div>')
                st.markdown(
                    f'<div class="rc" style="border-left-color:var({var});">'
                    f'<div class="rc-h">{r.name}<span class="rc-id">{r.rule_id}</span>'
                    f'{pill(SEVERITY_MEANING[r.severity], var)}</div>'
                    f'<div class="rc-m">{r.message}</div>{cmp_line}</div>',
                    unsafe_allow_html=True)

    skipped = [r for r in trace.stage_4_rules if r.status == RuleStatus.SKIP]
    if skipped:
        st.info(f"{mi('help_center')} **{len(skipped)} checks could not be run "
                f"at all.** They are recorded as *not checked* - never as "
                f"*passed*. A check that did not happen must never look like "
                f"one that succeeded.")


def money_panel(trace) -> None:
    fin = trace.stage_3_financials
    if fin is None:
        st.info(f"{mi('info')} No amounts were compared - no purchase order "
                f"was matched.")
        return
    c1, c2, c3 = st.columns(3)
    card_stat(c1, "handshake", "Agreed on the PO",
              format_paise(fin.po_total_paise), "before tax")
    card_stat(c2, "history", "Billed so far",
              format_paise(fin.already_invoiced_paise), "across earlier invoices")
    card_stat(c3, "account_balance_wallet", "Still available",
              format_paise(fin.remaining_balance_paise),
              "what this invoice is measured against")
    st.write("")
    c1, c2, c3 = st.columns(3)
    card_stat(c1, "request_quote", "This invoice",
              format_paise(fin.invoice_subtotal_paise), "before tax")
    card_stat(c2, "trending_up", "Difference", format_paise(fin.overage_paise),
              "under budget" if fin.is_under_billing else "over what was left")
    card_stat(c3, "rule", "Allowed to go over",
              format_paise(fin.allowed_overage_paise),
              "exceeded" if fin.is_breach else "not exceeded")

    if not fin.is_under_billing:
        var = "--bad" if fin.is_breach else "--ok"
        st.markdown(f"**Allowance used**  {fin.tolerance_consumption_pct:.0f}%"
                    + bar(fin.tolerance_consumption_pct, var),
                    unsafe_allow_html=True)

    bound = {"absolute_cap": f"the {format_paise(fin.cap_paise)} cap",
             "percentage": f"{config.TOLERANCE_PERCENT_DISPLAY}% of the PO",
             "equal": "both limits, which happen to be equal here"}[fin.binding_constraint]
    st.caption(
        f"**How the allowance is set.** A supplier may bill slightly over what "
        f"is left, up to whichever is *smaller*: "
        f"{config.TOLERANCE_PERCENT_DISPLAY}% of the purchase order "
        f"({format_paise(fin.percent_allowance_paise)}) or a flat "
        f"{format_paise(fin.cap_paise)}. Here the binding limit is **{bound}**.")


def reading_panel(trace) -> None:
    ex = trace.stage_1_extraction
    f, q = ex.fields, ex.quality
    tier_line = ("read the text already inside the PDF - no AI, no cost"
                 if ex.tier is Tier.FREE
                 else f"used a vision model ({ex.model}) to look at the page")
    st.caption(f"{mi('auto_awesome')} The **{ex.tier.value}** reader "
               f"{tier_line}." + (" Served from cache." if ex.cached else ""))
    if q and not q.passes_gate:
        st.warning(f"{mi('warning')} **This document could not be read.** "
                   f"{q.gate_reason}")
        return

    labels = {"invoice_number": "Invoice number", "invoice_date": "Invoice date",
              "vendor_name": "Supplier", "vendor_gstin": "Supplier tax number (GSTIN)",
              "po_reference": "Purchase order reference",
              "subtotal_paise": "Amount before tax", "gst_rate": "Tax rate",
              "tax_paise": "Tax", "total_paise": "Total"}
    money = {"subtotal_paise", "tax_paise", "total_paise"}
    rows = []
    for name, label in labels.items():
        fld = getattr(f, name)
        if fld.is_present:
            value = format_paise(fld.value) if name in money else str(fld.value)
            if name == "gst_rate":
                value = f"{fld.value}%"
            conf = f"{fld.confidence:.0%}"
        else:
            value = f"not found - {fld.reason or 'not on the document'}"
            conf = "-"
        rows.append({"What": label, "Value": value, "How sure": conf,
                     "Printed label": fld.found_as or "-"})
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption("**How sure** is the reader's own confidence. **Printed label** "
               "is the wording that actually appeared on the page, so a "
               "reviewer can find it in seconds.")
    if f.line_items:
        st.markdown("**Line items**")
        st.dataframe([{"Description": li.description, "Qty": li.qty,
                       "Unit price": format_paise(li.unit_price_paise) if li.unit_price_paise else "-",
                       "Amount": format_paise(li.amount_paise)} for li in f.line_items],
                     width="stretch", hide_index=True)
    for note in f.extraction_notes:
        st.caption(f"{mi('sticky_note_2')} {note}")


def matching_panel(trace) -> None:
    m = trace.stage_2_matching
    if m.po_number:
        how = ("The invoice printed the purchase order number, so there was "
               "nothing to work out." if m.match_layer == 1 else
               "No purchase order was printed, so the system worked it out from "
               "the supplier, the amount and the date.")
        st.success(f"{mi('link')} **{m.po_number}** - {how}")
    else:
        st.error(f"{mi('link_off')} No purchase order could be matched.")
    if m.vendor_id:
        st.caption(f"Supplier identified as **{m.vendor_name_matched}** "
                   f"({m.vendor_id}), name similarity {m.vendor_match_score}/100.")
    if m.candidates_considered:
        st.markdown("**Everything the system considered**")
        st.dataframe([{"Purchase order": c.po_number, "Score": c.score,
                       "Why": c.reason} for c in m.candidates_considered],
                     width="stretch", hide_index=True)
        st.caption("Options that were looked at and dismissed are recorded too, "
                   "not just the one chosen.")
    for note in m.notes:
        st.caption(note)


def checks_panel(trace) -> None:
    results = trace.stage_4_rules
    determining = set(trace.stage_5_decision.determined_by)
    choice = st.radio("Show", ["Only what went wrong", "All 33 checks"],
                      horizontal=True, label_visibility="collapsed")
    visible = (results if choice == "All 33 checks"
               else [r for r in results if r.status != RuleStatus.PASS])
    if not visible:
        st.success(f"{mi('task_alt')} Every check passed.")
        return
    st.dataframe([{"": "→" if r.rule_id in determining else "",
                   "Check": r.name, "Result": STATUS_LABEL[r.status],
                   "If it fails": SEVERITY_MEANING[r.severity],
                   "What happened": r.message, "Code": r.rule_id}
                  for r in visible], width="stretch", hide_index=True)
    st.caption("→ marks the check that decided the outcome. "
               "**Could not check** is never treated as **passed**.")


# ---------------------------------------------------------------------------
# VIEWS
# ---------------------------------------------------------------------------

def view_overview() -> None:
    st.markdown(
        f'<div class="band">'
        f'<span class="eyebrow">{ico("verified")} Invoice Guard</span>'
        f'<div class="band-h">From a PDF to a <em>reasoned decision</em></div>'
        f'<div class="band-p">An accounts payable team opens hundreds of '
        f'supplier invoices a month, finds the purchase order each one relates '
        f'to, checks the numbers line up, and decides what to do. It is '
        f'repetitive, and a tired person on a Friday afternoon makes expensive '
        f'mistakes.<br><br><b>This does the same job and shows its working '
        f'&mdash; every field it read, every option it considered, and the '
        f'exact check that decided the outcome.</b></div></div>',
        unsafe_allow_html=True)

    st.write("")
    c1, c2, _ = st.columns([1.1, 1.1, 3])
    with c1:
        if st.button(f"{mi('play_arrow')} Assess an invoice", type="primary",
                     width="stretch"):
            st.session_state.nav = "assess"
            st.rerun()
    with c2:
        if st.button(f"{mi('menu_book')} How it works", width="stretch"):
            st.session_state.nav = "learn"
            st.rerun()

    st.write("")
    c1, c2, c3, c4 = st.columns(4)
    card_stat(c1, "inventory_2", "Invoices in the set", str(len(corpus())),
              "18 digital PDFs, 3 scans")
    card_stat(c2, "checklist", "Checks per invoice", "33",
              "all run every time, none skipped early")
    card_stat(c3, "rule", "Tolerance",
              f"{config.TOLERANCE_PERCENT_DISPLAY}% or "
              f"{format_paise(config.TOLERANCE_ABSOLUTE_CAP_PAISE)}",
              "whichever is smaller", "--flag")
    card_stat(c4, "fact_check", "Assessed this session",
              str(len(st.session_state.order)), "traces written to disk",
              "--hold")

    st.write("")
    st.markdown(f"##### {mi('conveyor_belt')} What happens to every invoice")
    st.markdown(
        '<div class="flow">' + "".join(
            f'<div class="flow-i"><div class="flow-n">Step {n}</div>'
            f'<div class="flow-t">{ico(icon)} {title}</div>'
            f'<div class="flow-d">{why.split(".")[0]}.</div></div>'
            for n, (title, icon, why) in STAGES.items()
        ) + '</div>', unsafe_allow_html=True)

    st.write("")
    st.markdown(
        f'<div class="callout"><div class="callout-h">{ico("bolt")} '
        f'The AI reads. The code decides.</div>'
        f'<div class="callout-b">A model is used for exactly one thing: turning '
        f'an invoice into structured numbers. Every judgement after that is '
        f'ordinary programming. The same invoice always produces the same '
        f'answer, and any outcome traces back to the specific check that caused '
        f'it. In a payments system, an approval that cannot be reproduced is '
        f'not an approval &mdash; it is a guess.</div></div>',
        unsafe_allow_html=True)

    st.write("")
    st.markdown(f"##### {mi('alt_route')} And one of four things happens")
    cols = st.columns(4)
    for col, d in zip(cols, Decision):
        ui = DECISION_UI[d]
        col.markdown(
            f'<div class="oc" style="border-top-color:var({ui["var"]});">'
            f'<div class="oc-t" style="color:var({ui["var"]});">'
            f'{ico(ui["icon"])} {ui["label"]}</div>'
            f'<div class="oc-d">{ui["what"]}</div></div>',
            unsafe_allow_html=True)

    st.write("")
    st.caption("No amount, however large, causes an automatic rejection. An "
               "invoice well over its purchase order might be fraud, or an "
               "agreed change nobody wrote down &mdash; the system cannot tell, "
               "so it holds and asks rather than pretending to know.")


def view_assess(tier: Tier) -> None:
    files = corpus()
    st.markdown(f"### {mi('bolt')} Assess an invoice")

    left, right = st.columns([4, 1])
    with left:
        chosen = st.selectbox("Invoice", [p.stem for p in files],
                              label_visibility="collapsed")
    with right:
        go = st.button(f"{mi('play_arrow')} Assess", type="primary",
                       width="stretch")
    uploaded = st.file_uploader("Or upload your own PDF", type=["pdf"])

    chosen_path = next(p for p in files if p.stem == chosen)

    target = None
    if go:
        target = chosen_path
    elif uploaded is not None:
        tmp = config.ROOT_DIR / ".cache" / uploaded.name
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(uploaded.getbuffer())
        target = tmp

    if target is None:
        c1, c2 = st.columns([1, 1.6])
        with c1:
            png = preview_of(chosen_path)
            if png:
                st.image(png, caption=chosen_path.name, width="stretch")
        with c2:
            st.markdown(
                f'<div class="empty"><div style="font-size:34px;'
                f'color:var(--accent);">{ico("touch_app")}</div>'
                f'<div style="font-size:1.1rem;font-weight:650;margin-top:8px;">'
                f'Ready when you are</div>'
                f'<div style="opacity:.7;margin-top:6px;max-width:46ch;'
                f'margin-left:auto;margin-right:auto;">Press <b>Assess</b> and '
                f'you will see each step of the reasoning as it happens - what '
                f'was read, which purchase order it matched, how the numbers '
                f'compared, and which check decided the outcome.</div></div>',
                unsafe_allow_html=True)
        return

    st.markdown(f"##### {mi('conveyor_belt')} What the system is doing")
    prev_col, step_col = st.columns([1, 2])
    with prev_col:
        png = preview_of(target)
        if png:
            st.image(png, caption=Path(target).name, width="stretch")
    with step_col:
        slot = st.empty()
        try:
            trace = run_with_narration(target, tier, slot)
        except Exception as exc:
            st.error(f"Could not process this invoice on the {tier.value} "
                     f"reader.\n\n{exc}")
            if tier is Tier.PREMIUM:
                st.info(f"The premium reader needs the `openai` package and "
                        f"`{config.OPENAI_API_KEY_ENV}` in `.env`. The free "
                        f"reader needs neither.")
            return

    remember(target, trace)
    st.write("")
    hero(trace)
    st.write("")
    key_facts(trace)
    st.write("")
    why_panel(trace)

    st.divider()
    st.markdown(f"##### {mi('travel_explore')} The evidence behind it")
    t1, t2, t3, t4 = st.tabs(["What was read", "The purchase order",
                              "The numbers", "All 33 checks"])
    with t1:
        reading_panel(trace)
    with t2:
        matching_panel(trace)
    with t3:
        money_panel(trace)
    with t4:
        checks_panel(trace)

    st.download_button(f"{mi('download')} Download the full record (JSON)",
                       trace.model_dump_json(indent=2),
                       file_name=f"{trace.trace_id}.json",
                       mime="application/json")


def view_queue() -> None:
    st.markdown(f"### {mi('list_alt')} Everything assessed")
    if not st.session_state.order:
        st.markdown(
            f'<div class="empty"><div style="font-size:34px;opacity:.5;">'
            f'{ico("inbox")}</div><div style="font-size:1.05rem;'
            f'font-weight:650;margin-top:8px;">Nothing assessed yet</div>'
            f'<div style="opacity:.7;margin-top:6px;">Assess one invoice, or '
            f'run the whole set from the sidebar.</div></div>',
            unsafe_allow_html=True)
        return

    traces = [st.session_state.traces[s] for s in st.session_state.order]
    total = len(traces)
    cols = st.columns(4)
    for col, decision in zip(cols, Decision):
        n = sum(1 for t in traces if t.stage_5_decision.decision == decision)
        ui = DECISION_UI[decision]
        col.markdown(
            f'<div class="card" style="background:var({ui["var"]}-soft);'
            f'border-color:var({ui["var"]});">'
            f'<div class="stat-k" style="color:var({ui["var"]});">'
            f'{ico(ui["icon"])} {ui["label"]}</div>'
            f'<div class="stat-v" style="color:var({ui["var"]});">{n}</div>'
            + bar(100 * n / total if total else 0, ui["var"]) + '</div>',
            unsafe_allow_html=True)

    st.write("")
    st.dataframe(
        [{"Invoice": s,
          "Outcome": DECISION_UI[st.session_state.traces[s].stage_5_decision.decision]["label"],
          "Why": ", ".join(st.session_state.traces[s].stage_5_decision.determined_by) or "everything passed",
          "Purchase order": st.session_state.traces[s].stage_2_matching.po_number or "-",
          "Amount": (format_paise(v) if (v := st.session_state.traces[s]
                     .stage_1_extraction.fields.subtotal_paise.value) is not None else "-"),
          "Reader": st.session_state.traces[s].stage_1_extraction.tier.value}
         for s in st.session_state.order],
        width="stretch", hide_index=True)


def _traces_for_po(po_number: str) -> list:
    """Invoices assessed in THIS session that matched a given PO, in order."""
    return [
        (stem, st.session_state.traces[stem])
        for stem in st.session_state.order
        if st.session_state.traces[stem].stage_2_matching.po_number == po_number
    ]


def credentials_panel() -> None:
    """
    Collect premium-reader credentials from whoever is using the app.

    Values are mirrored into st.session_state["creds"], a plain dict rather
    than widget state. Widget state is garbage-collected when a run aborts --
    which every navigation does -- so a typed key would otherwise vanish the
    moment someone clicked away.

    NOTHING HERE IS WRITTEN TO DISK. It lasts for the session and then goes
    away, which is the right default for someone else's secret. It also never
    reaches a trace, a log line, or an error message.
    """
    store_ = st.session_state.creds
    env = cred.from_env()

    st.caption("The premium reader needs a key. Nothing typed here is saved "
               "to disk \u2014 it lasts for this session only.")

    def remember(name: str, value: str) -> str:
        store_[name] = value
        return value

    def seeded(name: str, fallback: str | None = None) -> str:
        return store_.get(name) or (fallback or "")

    routes = ["OpenAI direct", "Portkey gateway"]
    default_route = store_.get("route") or (
        routes[1] if env.route == cred.PORTKEY else routes[0])
    route_label = st.radio(
        "Route", routes, key="cred_route_widget",
        index=routes.index(default_route), label_visibility="collapsed",
        captions=["Your own OpenAI key.",
                  "An organisation gateway that holds the provider key."])
    store_["route"] = route_label
    route = cred.PORTKEY if route_label.startswith("Portkey") else cred.OPENAI

    if route is cred.OPENAI:
        api_key = remember("openai_key", st.text_input(
            "OpenAI API key", type="password", key="w_openai_key",
            value=seeded("openai_key",
                         env.api_key if env.route == cred.OPENAI else None),
            placeholder="sk-..."))
        model = remember("openai_model", st.text_input(
            "Model", key="w_openai_model",
            value=seeded("openai_model", env.model or config.OPENAI_VISION_MODEL)))
        creds = cred.Credentials(route=cred.OPENAI, api_key=api_key or None,
                                 model=model or None)
    else:
        api_key = remember("pk_key", st.text_input(
            "Portkey API key", type="password", key="w_pk_key",
            value=seeded("pk_key",
                         env.api_key if env.route == cred.PORTKEY else None),
            placeholder="pk-...",
            help="The gateway key. The provider's own key stays in Portkey."))
        base_url = remember("pk_base", st.text_input(
            "Gateway URL", key="w_pk_base",
            value=seeded("pk_base", env.base_url or config.PORTKEY_BASE_URL),
            help="Change this if your organisation runs its own gateway."))
        st.caption("Portkey needs to know which provider to route to. "
                   "Fill in whichever one your setup uses.")
        provider = remember("pk_provider", st.text_input(
            "Provider", key="w_pk_provider",
            value=seeded("pk_provider", env.provider),
            placeholder="@openai-prod"))
        virtual_key = remember("pk_vk", st.text_input(
            "Virtual key", type="password", key="w_pk_vk",
            value=seeded("pk_vk", env.virtual_key),
            help="The older equivalent of a provider. Still supported."))
        config_id = remember("pk_config", st.text_input(
            "Config id", key="w_pk_config",
            value=seeded("pk_config", env.config_id),
            help="A saved Portkey config, if you use one."))
        model = remember("pk_model", st.text_input(
            "Model", key="w_pk_model",
            value=seeded("pk_model", env.model or config.OPENAI_VISION_MODEL)))
        creds = cred.Credentials(
            route=cred.PORTKEY, api_key=api_key or None,
            base_url=base_url or None, provider=provider or None,
            virtual_key=virtual_key or None, config_id=config_id or None,
            model=model or None)

    cred.set_credentials(creds)

    problem = creds.missing()
    if problem:
        st.warning(problem)
    else:
        st.success("Ready to use the premium reader.")

    if st.button(f"{mi('network_check')} Test the connection", width="stretch"):
        with st.spinner("Sending one tiny request..."):
            ok, message = cred.probe(creds)
        (st.success if ok else st.error)(message)
        if not ok:
            st.caption("A failure here is the connection, not your invoices. "
                       "The free reader keeps working regardless.")


def restore_credentials() -> None:
    """
    Re-attach credentials on a run where the sidebar panel did not render.

    A navigation aborts the script before the sidebar is built, so without this
    the extractor would find no credentials on exactly the run that needs them.
    """
    store_ = st.session_state.get("creds") or {}
    if not store_:
        return
    if (store_.get("route") or "").startswith("Portkey"):
        cred.set_credentials(cred.Credentials(
            route=cred.PORTKEY, api_key=store_.get("pk_key") or None,
            base_url=store_.get("pk_base") or None,
            provider=store_.get("pk_provider") or None,
            virtual_key=store_.get("pk_vk") or None,
            config_id=store_.get("pk_config") or None,
            model=store_.get("pk_model") or None))
    else:
        cred.set_credentials(cred.Credentials(
            route=cred.OPENAI, api_key=store_.get("openai_key") or None,
            model=store_.get("openai_model") or None))


def view_orders() -> None:
    st.markdown(f"### {mi('receipt_long')} Purchase orders")
    st.caption("What the company agreed to buy, before any invoice arrived. "
               "Every decision the system makes is ultimately \u201cdoes this "
               "invoice agree with what we already committed to?\u201d")

    pos = store.get_all_pos()
    vendors = {v.vendor_id: v for v in store.get_all_vendors()}

    committed = sum(p.po_total_paise for p in pos)
    billed = sum(p.already_invoiced_paise for p in pos)
    open_n = sum(1 for p in pos if p.is_open)

    c1, c2, c3, c4 = st.columns(4)
    card_stat(c1, "inventory_2", "Purchase orders", str(len(pos)),
              f"{open_n} open, {len(pos) - open_n} closed or cancelled")
    card_stat(c2, "handshake", "Total committed", format_paise(committed),
              "before tax, across every PO")
    card_stat(c3, "history", "Billed against them", format_paise(billed),
              f"{100 * billed / committed:.0f}% of the commitment", "--flag")
    card_stat(c4, "account_balance_wallet", "Still available",
              format_paise(committed - billed),
              "what remains to be invoiced", "--hold")

    st.write("")
    all_statuses = ["OPEN", "CLOSED", "CANCELLED"]
    # Seed once through session_state rather than passing `default` alongside
    # `key`. Streamlit ignores `default` as soon as the key exists, so the two
    # together are fragile: any loss of widget state leaves the filter empty
    # instead of restoring it.
    st.session_state.setdefault("po_status", list(all_statuses))

    f1, f2 = st.columns([1, 1])
    with f1:
        chosen_statuses = st.multiselect("Status", all_statuses, key="po_status")
    with f2:
        names = ["All suppliers"] + sorted({p.vendor_name for p in pos})
        who = st.selectbox("Supplier", names, key="po_supplier")

    # An empty status filter means "no constraint", not "show nothing".
    # Nobody deliberately filters down to zero rows, and a blank page with no
    # explanation is the worst possible answer to an accidental clear.
    statuses = chosen_statuses or all_statuses
    if not chosen_statuses:
        st.caption("No status selected, so all of them are shown.")

    shown = [p for p in pos
             if p.status.value in statuses
             and (who == "All suppliers" or p.vendor_name == who)]

    if not shown:
        st.warning(f"No purchase orders for **{who}** with the selected "
                   f"status. Widen the filters to see more.")
        return

    rows = []
    for p in shown:
        allow = allowed_overage(p.po_total_paise)
        used = (100 * p.already_invoiced_paise / p.po_total_paise
                if p.po_total_paise else 0)
        v = vendors.get(p.vendor_id)
        rows.append({
            "PO": p.po_number,
            "Supplier": p.vendor_name,
            "Approved supplier": "yes" if v and v.is_approved else "NO",
            "Raised": p.po_date.isoformat(),
            "Valid until": p.valid_until.isoformat() if p.valid_until else "-",
            "Status": p.status.value,
            "PO total": format_paise(p.po_total_paise),
            "Billed": format_paise(p.already_invoiced_paise),
            "Remaining": format_paise(p.remaining_balance_paise),
            "Used %": round(used, 1),
            "Over-billing allowed": format_paise(allow.allowed_paise),
            "Limit set by": ("cash cap" if allow.binding_constraint == "absolute_cap"
                             else "percentage" if allow.binding_constraint == "percentage"
                             else "both"),
            "GST": f"{p.expected_gst_rate}%" if p.expected_gst_rate is not None else "-",
            "For": p.description or "-",
        })

    st.dataframe(
        rows, width="stretch", hide_index=True,
        column_config={
            "Used %": st.column_config.ProgressColumn(
                "Used", min_value=0, max_value=100, format="%.0f%%"),
        })
    st.caption("Balances are live \u2014 they move as invoices are accepted in "
               "this session. **Over-billing allowed** is "
               f"min({config.TOLERANCE_PERCENT_DISPLAY}% of the PO, "
               f"{format_paise(config.TOLERANCE_ABSOLUTE_CAP_PAISE)}), which is "
               "why the binding limit differs between small and large orders.")

    st.divider()
    st.markdown(f"##### {mi('search')} Inspect one")
    numbers = [p.po_number for p in shown]
    by_number = {p.po_number: p for p in shown}
    # If the filters moved and the previously inspected PO is no longer listed,
    # fall back to the first one rather than raising on a stale selection.
    prior = st.session_state.get("po_inspect")
    picked = st.selectbox(
        "Purchase order", numbers,
        index=numbers.index(prior) if prior in numbers else 0,
        format_func=lambda n: f"{n}  \u2014  {by_number[n].vendor_name}",
        key="po_inspect_widget", label_visibility="collapsed")
    st.session_state.po_inspect = picked
    po = by_number[picked]
    v = vendors.get(po.vendor_id)
    allow = allowed_overage(po.po_total_paise)

    ceiling = po.po_total_paise + allow.allowed_paise
    tone = "--ok" if po.is_open else "--bad"
    st.markdown(
        f'<div class="hero" style="background:var({tone}-soft);'
        f'border-color:var({tone});">'
        f'<div class="hero-ico" style="background:var({tone}-soft);'
        f'color:var({tone});">{ico("receipt_long")}</div><div style="flex:1;">'
        f'<div class="hero-t" style="color:var({tone});">{po.po_number}'
        f'{pill(po.status.value.title(), tone)}</div>'
        f'<div class="hero-w">{po.description or "No description recorded."}<br>'
        f'{po.vendor_name} &middot; raised {po.po_date}'
        f'{" &middot; valid until " + str(po.valid_until) if po.valid_until else ""}'
        f'</div>'
        f'<div class="hero-m">Anything billed above '
        f'{format_paise(ceiling)} in total breaches this order &mdash; '
        f'{format_paise(po.po_total_paise)} committed plus '
        f'{format_paise(allow.allowed_paise)} tolerance.</div></div></div>',
        unsafe_allow_html=True)

    st.write("")
    c1, c2, c3, c4 = st.columns(4)
    card_stat(c1, "handshake", "Committed", format_paise(po.po_total_paise),
              "before tax")
    card_stat(c2, "history", "Billed so far",
              format_paise(po.already_invoiced_paise),
              f"{100 * po.already_invoiced_paise / po.po_total_paise:.0f}% used"
              if po.po_total_paise else "-", "--flag")
    card_stat(c3, "account_balance_wallet", "Remaining",
              format_paise(po.remaining_balance_paise),
              "still billable", "--hold")
    card_stat(c4, "rule", "Over-billing allowed",
              format_paise(allow.allowed_paise),
              ("the cash cap binds here"
               if allow.binding_constraint == "absolute_cap"
               else f"{config.TOLERANCE_PERCENT_DISPLAY}% binds here"
               if allow.binding_constraint == "percentage"
               else "both limits coincide"))

    pct = (100 * po.already_invoiced_paise / po.po_total_paise
           if po.po_total_paise else 0)
    st.markdown(
        f"**Consumption**  {pct:.0f}% of {format_paise(po.po_total_paise)}"
        + bar(pct, "--bad" if pct > 100 else "--ok"),
        unsafe_allow_html=True)

    st.caption(
        f"The tolerance is min({config.TOLERANCE_PERCENT_DISPLAY}% of "
        f"{format_paise(po.po_total_paise)} = "
        f"{format_paise(allow.percent_allowance_paise)}, cap "
        f"{format_paise(allow.cap_paise)}) = "
        f"**{format_paise(allow.allowed_paise)}**. It is measured against the "
        f"REMAINING balance, not the PO total \u2014 which is what stops a "
        f"supplier splitting one order into several invoices and collecting a "
        f"fresh allowance each time.")

    if v:
        st.write("")
        st.markdown(f"**{mi('storefront')} Supplier on record**")
        approved = ("on the approved list" if v.is_approved
                    else "**NOT approved** \u2014 nothing from this supplier can be paid")
        st.caption(f"{v.legal_name} ({v.vendor_id}) \u2014 {approved}. "
                   f"GSTIN {v.gstin or 'not recorded'}. "
                   f"Also invoices as: {', '.join(v.aliases) or 'no aliases recorded'}.")

    st.write("")
    st.markdown(f"**{mi('receipt')} Invoices assessed against this order**")
    hits = _traces_for_po(po.po_number)
    if not hits:
        st.caption("None assessed in this session yet. Assess an invoice, or "
                   "run the whole set from the sidebar, and it will appear here.")
        return

    st.dataframe(
        [{"Invoice": stem,
          "Outcome": DECISION_UI[t.stage_5_decision.decision]["label"],
          "Why": ", ".join(t.stage_5_decision.determined_by) or "everything passed",
          "Billed": (format_paise(x) if (x := t.stage_1_extraction.fields
                     .subtotal_paise.value) is not None else "-"),
          "Counted against the PO":
              "yes" if t.stage_5_decision.decision in
              (Decision.AUTO_APPROVE, Decision.APPROVE_WITH_FLAG) else "no",
          "Remaining after":
              (format_paise(t.stage_3_financials.remaining_balance_paise
                            - t.stage_3_financials.invoice_subtotal_paise)
               if t.stage_3_financials and t.stage_5_decision.decision in
               (Decision.AUTO_APPROVE, Decision.APPROVE_WITH_FLAG)
               else "unchanged")}
         for stem, t in hits],
        width="stretch", hide_index=True)
    st.caption("Only accepted invoices consume the balance. A held or rejected "
               "invoice must not eat budget it was never approved for \u2014 "
               "otherwise a queue of pending reviews would silently exhaust "
               "the order.")


def view_learn() -> None:
    st.markdown(f"### {mi('menu_book')} How this works")

    st.markdown(f"##### {mi('alt_route')} The four possible outcomes")
    for d in Decision:
        ui = DECISION_UI[d]
        st.markdown(
            f'<div class="rc" style="border-left-color:var({ui["var"]});">'
            f'<div class="rc-h" style="color:var({ui["var"]});">'
            f'{ico(ui["icon"])} {ui["label"]}</div>'
            f'<div class="rc-m">{ui["what"]}</div></div>',
            unsafe_allow_html=True)
    st.caption("No amount, however large, causes an automatic rejection. An "
               "invoice 40% over its purchase order might be fraud, or an "
               "agreed change nobody wrote down. The system cannot tell, so it "
               "does not pretend to - it holds and asks. Rejection is kept for "
               "things wrong regardless of context: no purchase order, an "
               "unapproved supplier, a closed order, a confirmed duplicate.")

    st.write("")
    st.markdown(f"##### {mi('percent')} How much over-billing is allowed")
    c1, c2, c3 = st.columns(3)
    card_stat(c1, "percent", "Percentage limit",
              f"{config.TOLERANCE_PERCENT_DISPLAY}%", "of the purchase order value")
    card_stat(c2, "payments", "Cash limit",
              format_paise(config.TOLERANCE_ABSOLUTE_CAP_PAISE),
              "flat cap, whatever the size")
    card_stat(c3, "compare_arrows", "Whichever is smaller", "both apply",
              f"they cross at {format_paise(config.TOLERANCE_CROSSOVER_PAISE)}")
    st.caption("On a small order the percentage is tighter. On a large one the "
               "flat cap takes over. An invoice can be under one limit and over "
               "the other - exactly the case that catches out a system using a "
               "single threshold.")

    st.write("")
    st.markdown(f"##### {mi('layers')} The two readers")
    c1, c2 = st.columns(2)
    c1.markdown(
        f'<div class="card"><div class="stat-k">{ico("bolt")} Free</div>'
        f'<div class="stat-n" style="font-size:.92rem;margin-top:8px;">Reads '
        f'the text layer digital PDFs already carry. Exact, instant, costs '
        f'nothing. Cannot read a scan, because a scan is a picture with no '
        f'text in it. When it meets one it says so and asks for the premium '
        f'reader rather than guessing.</div></div>', unsafe_allow_html=True)
    c2.markdown(
        f'<div class="card"><div class="stat-k">{ico("auto_awesome")} Premium'
        f'</div><div class="stat-n" style="font-size:.92rem;margin-top:8px;">'
        f'Sends the page to a vision model. Handles scans, rotation, poor '
        f'quality. Costs a fraction of a rupee per page. Everything after '
        f'reading is identical - the same checks, the same rules, the same '
        f'decisions.</div></div>', unsafe_allow_html=True)

    st.write("")
    st.markdown(f"##### {mi('checklist')} Every check the system runs")
    st.caption("All 33 run on every invoice. Nothing stops early. A check whose "
               "prerequisites are missing reports *not checked*, which is never "
               "the same as *passed*.")
    st.dataframe([{"Check": r["name"],
                   "If it fails": SEVERITY_MEANING[Severity(r["severity"])],
                   "Code": r["rule_id"]} for r in rule_catalogue()],
                 width="stretch", hide_index=True, height=400)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    init_state()
    st.markdown(CSS, unsafe_allow_html=True)

    with st.sidebar:
        st.markdown(
            f'<div class="brand"><div class="brand-dot">'
            f'{ico("receipt_long")}</div>Invoice Decisioning</div>',
            unsafe_allow_html=True)

        for key, label, icon in NAV:
            active = st.session_state.nav == key
            if active:
                st.markdown('<div class="navactive">', unsafe_allow_html=True)
            if st.button(f"{mi(icon)}  {label}", key=f"nav_{key}"):
                st.session_state.nav = key
                st.rerun()
            if active:
                st.markdown('</div>', unsafe_allow_html=True)

        st.divider()
        st.markdown(f"**{mi('tune')} How to read the invoice**")
        # The nav buttons above call st.rerun(), which aborts the script
        # before this radio is constructed. Streamlit garbage-collects the
        # session state of any keyed widget that did not render in a run, so a
        # widget key alone is NOT enough -- the choice would silently revert to
        # Free on every navigation. Mirroring into a plain (non-widget) key
        # survives the abort.
        tier_label = st.radio(
            "Reader", ["Free", "Premium"], key="tier_widget",
            index=["Free", "Premium"].index(st.session_state.tier_choice),
            label_visibility="collapsed",
            captions=["Digital PDFs. Instant, no cost.",
                      "Any document, including scans."])
        st.session_state.tier_choice = tier_label
        tier = Tier.FREE if tier_label == "Free" else Tier.PREMIUM

        if tier is Tier.PREMIUM:
            credentials_panel()
        else:
            # Detach runtime credentials when the premium reader is not
            # selected, so a stale key cannot be used by accident.
            cred.clear_credentials()
            st.session_state.creds = {}

        st.divider()
        if st.button(f"{mi('playlist_add_check')} Assess all "
                     f"{len(corpus())} invoices", width="stretch"):
            reset_everything()
            bar_w = st.progress(0.0, text="Starting...")
            files = corpus()
            failed = []
            for i, path in enumerate(files, 1):
                try:
                    remember(path, process_invoice(path, tier, today=BATCH_DATE))
                except Exception as exc:
                    failed.append((path.stem, str(exc)))
                bar_w.progress(i / len(files), text=f"{path.stem} ({i}/{len(files)})")
            bar_w.empty()
            st.success(f"Assessed {len(files) - len(failed)} of {len(files)}.")
            for stem, err in failed:
                st.error(f"{stem}: {err}")
            st.session_state.nav = "queue"
            st.rerun()

        if st.button(f"{mi('restart_alt')} Start over", width="stretch"):
            reset_everything()
            st.success("Reset to the starting position.")
        st.caption("Purchase orders remember what has already been billed "
                   "against them, so a fresh start matters before re-running.")

    if tier is Tier.PREMIUM:
        restore_credentials()

    {"overview": view_overview, "assess": lambda: view_assess(tier),
     "orders": view_orders, "queue": view_queue,
     "learn": view_learn}[st.session_state.nav]()


if __name__ == "__main__":
    main()
