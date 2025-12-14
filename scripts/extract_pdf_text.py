import argparse
import sys
from pathlib import Path


def extract_with_pypdf(pdf_path: Path, *, max_pages: int | None = None) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "Missing dependency 'pypdf'. Install with: python -m pip install pypdf"
        ) from e

    reader = PdfReader(str(pdf_path))
    parts: list[str] = []
    for i, page in enumerate(reader.pages, 1):
        if max_pages is not None and i > max_pages:
            break
        text = page.extract_text() or ""
        parts.append(f"\n\n=== Page {i} ===\n")
        parts.append(text)
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract text from a PDF (local only).")
    parser.add_argument("pdf", help="Path to PDF")
    parser.add_argument(
        "--out",
        default="",
        help="Optional output .txt path. If omitted, prints only stats + preview.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum pages to extract (0 = all).",
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=1200,
        help="Preview chars to print to stdout.",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="Do not print extracted text preview.",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    max_pages = args.max_pages if args.max_pages and args.max_pages > 0 else None
    text = extract_with_pypdf(pdf_path, max_pages=max_pages)

    print(f"extracted_chars={len(text)}")
    if args.no_preview:
        print("preview=disabled")
    else:
        preview = text[: max(0, args.preview_chars)]
        print(f"preview_chars={len(preview)}")
        print("--- preview ---")
        print(preview)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote={out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
