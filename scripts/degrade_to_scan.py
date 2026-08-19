"""
Turn selected clean PDFs into realistic scanned images.

    python -m scripts.degrade_to_scan

Reads the invoices marked "scan" in invoice_specs.py, renders each to a
low-resolution image, degrades it the way a real office scanner would, and
wraps it back into a PDF in data/invoices/scanned/.

The clean original is then REMOVED from data/invoices/clean/ so the batch does
not process the same invoice twice. Re-running scripts.generate_invoices
restores it, so the cycle is repeatable.

Pass --keep to leave the clean originals in place.

Degradation applied, in the order a real scan introduces it:
  1. Render at low DPI                (scanner resolution)
  2. Rotate slightly                  (paper not square on the glass)
  3. Reduce contrast                  (worn toner, cheap sensor)
  4. Blur                             (imperfect focus)
  5. Add gaussian noise               (sensor noise)
  6. Add a light edge gradient        (lid not fully closed)
"""

import argparse
import io

import numpy as np
import pymupdf
from PIL import Image, ImageEnhance, ImageFilter

from src import config
from scripts.invoice_specs import INVOICES

# Normal scan: legible, but clearly not a born-digital PDF.
NORMAL = {
    "dpi": 110,
    "rotate": 1.1,
    "contrast": 0.90,
    "blur": 0.45,
    "noise": 9.0,
    "vignette": 0.06,
}

# Heavy scan (EC-4): everything still readable EXCEPT the faint invoice number.
HEAVY = {
    "dpi": 82,
    "rotate": 2.2,
    "contrast": 0.78,
    "blur": 0.95,
    "noise": 20.0,
    "vignette": 0.14,
}


def render(pdf_path, dpi: int) -> Image.Image:
    doc = pymupdf.open(pdf_path)
    pix = doc[0].get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    doc.close()
    return img


def add_vignette(img: Image.Image, strength: float) -> Image.Image:
    """Darken toward the edges, the way a lifted scanner lid does."""
    a = np.asarray(img).astype(np.float32)
    h, w = a.shape
    yy = np.linspace(-1.0, 1.0, h)[:, None]
    xx = np.linspace(-1.0, 1.0, w)[None, :]
    radial = np.sqrt(xx ** 2 + yy ** 2) / np.sqrt(2.0)
    a *= 1.0 - strength * radial
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def add_noise(img: Image.Image, sigma: float) -> Image.Image:
    a = np.asarray(img).astype(np.float32)
    rng = np.random.default_rng(42)  # fixed seed -> repeatable output
    a += rng.normal(0.0, sigma, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def degrade(pdf_path, profile: dict) -> Image.Image:
    img = render(pdf_path, profile["dpi"])
    img = img.rotate(
        profile["rotate"], resample=Image.BICUBIC, expand=False, fillcolor=248
    )
    img = ImageEnhance.Contrast(img).enhance(profile["contrast"])
    img = img.filter(ImageFilter.GaussianBlur(profile["blur"]))
    img = add_noise(img, profile["noise"])
    img = add_vignette(img, profile["vignette"])
    return img


def image_to_pdf(img: Image.Image, out_path) -> None:
    """Wrap the degraded image back into a single-page PDF."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=72)
    buf.seek(0)

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4 points
    page.insert_image(pymupdf.Rect(0, 0, 595, 842), stream=buf.read())
    doc.save(str(out_path))
    doc.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="leave the clean originals in place")
    args = ap.parse_args()

    src_dir = config.CLEAN_INVOICE_DIR
    out_dir = config.SCANNED_INVOICE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = [s for s in INVOICES if s.get("scan")]
    if not targets:
        raise SystemExit("No invoices are marked for scanning.")

    for spec in targets:
        stem = spec["stem"]
        src = src_dir / f"{stem}.pdf"
        if not src.exists():
            raise SystemExit(
                f"{src.name} not found. Run: python -m scripts.generate_invoices"
            )

        heavy = spec.get("heavy_degrade", False)
        profile = HEAVY if heavy else NORMAL

        img = degrade(src, profile)
        dst = out_dir / f"{stem}.pdf"
        image_to_pdf(img, dst)

        label = "HEAVY" if heavy else "normal"
        print(f"  {stem:<20} {label:<7} {profile['dpi']} dpi  "
              f"rot {profile['rotate']}deg  noise {profile['noise']}  "
              f"-> {dst.relative_to(config.ROOT_DIR)}")

        if not args.keep:
            src.unlink()

    clean_n = len(list(src_dir.glob("*.pdf")))
    scan_n = len(list(out_dir.glob("*.pdf")))
    print(f"\n  clean/   {clean_n} invoices")
    print(f"  scanned/ {scan_n} invoices")
    print(f"  total    {clean_n + scan_n}")
    if not args.keep:
        print("\n  Clean originals removed so the batch does not double-count.")
        print("  Re-run scripts.generate_invoices to restore them.")


if __name__ == "__main__":
    main()
