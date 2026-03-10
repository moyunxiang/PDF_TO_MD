#!/usr/bin/env python3
"""
Split a large PDF into smaller PDFs by chapters or page count.
Pure splitting only — no conversion.

Output goes to pdf/{name}/ directory.

Usage (via main.py):
    python main.py split textbook.pdf              # Auto-detect bookmarks
    python main.py split textbook.pdf --pages 50   # Split every 50 pages
"""

import re
import sys
from pathlib import Path

import pypdfium2
from rich.panel import Panel
from rich.table import Table

from convert import (
    console,
    select_menu,
    _format_size,
    _quick_page_count,
    PDF_DIR,
)

# ── TOC / Bookmark Reading ───────────────────────────────────────

def _read_toc(pdf_path: Path) -> list[dict]:
    """Read PDF bookmarks/TOC. Returns list of {title, start_page, end_page, level, num_pages}."""
    doc = pypdfium2.PdfDocument(str(pdf_path))
    total_pages = len(doc)

    raw_items = []
    try:
        for item in doc.get_toc():
            page_idx = item.page_index
            if page_idx is None or page_idx < 0:
                continue
            raw_items.append({
                "title": (item.title or "").strip(),
                "start_page": page_idx,
                "level": item.level,
            })
    except Exception:
        pass
    finally:
        doc.close()

    if not raw_items:
        return []

    # Keep only top-level (smallest level number) bookmarks
    min_level = min(item["level"] for item in raw_items)
    top_items = [item for item in raw_items if item["level"] == min_level]

    # Calculate end pages
    chapters = []
    for i, item in enumerate(top_items):
        end_page = (top_items[i + 1]["start_page"] if i + 1 < len(top_items) else total_pages)
        chapters.append({
            "title": item["title"],
            "start_page": item["start_page"],
            "end_page": end_page,
            "level": item["level"],
            "num_pages": end_page - item["start_page"],
        })

    return chapters


# ── PDF Splitting ────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Convert text to filesystem-friendly slug."""
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text[:60] if text else "untitled"


def _split_pdf_by_toc(pdf_path: Path, chapters: list[dict], out_dir: Path) -> list[dict]:
    """Split a PDF into chapter PDFs based on TOC bookmarks."""
    doc = pypdfium2.PdfDocument(str(pdf_path))
    results = []

    for i, ch in enumerate(chapters, 1):
        slug = _slugify(ch["title"])
        chunk_name = f"ch{i:02d}-{slug}"
        chunk_pdf = out_dir / f"{chunk_name}.pdf"

        new_doc = pypdfium2.PdfDocument.new()
        new_doc.import_pages(doc, list(range(ch["start_page"], ch["end_page"])))
        new_doc.save(str(chunk_pdf))
        new_doc.close()

        results.append({
            "pdf_path": chunk_pdf,
            "title": ch["title"],
            "name": chunk_name,
            "pages": ch["num_pages"],
            "start": ch["start_page"] + 1,
            "end": ch["end_page"],
        })

    doc.close()
    return results


def _split_pdf_by_pages(pdf_path: Path, pages_per_chunk: int, out_dir: Path) -> list[dict]:
    """Split a PDF into chunks of N pages each."""
    doc = pypdfium2.PdfDocument(str(pdf_path))
    total = len(doc)
    results = []

    for start in range(0, total, pages_per_chunk):
        end = min(start + pages_per_chunk, total)
        i = start // pages_per_chunk + 1
        chunk_name = f"part{i:02d}-p{start+1}-{end}"
        chunk_pdf = out_dir / f"{chunk_name}.pdf"

        new_doc = pypdfium2.PdfDocument.new()
        new_doc.import_pages(doc, list(range(start, end)))
        new_doc.save(str(chunk_pdf))
        new_doc.close()

        results.append({
            "pdf_path": chunk_pdf,
            "title": f"Pages {start+1}\u2013{end}",
            "name": chunk_name,
            "pages": end - start,
            "start": start + 1,
            "end": end,
        })

    doc.close()
    return results


# ── Main Split Flow ──────────────────────────────────────────────

def split_pdf(pdf_path: Path, pages_per_chunk: int | None = None):
    """Main entry: split a PDF into smaller PDFs (no conversion).

    Output goes to pdf/{stem}/ directory.
    """
    total_pages = _quick_page_count(pdf_path)
    pdf_size = pdf_path.stat().st_size
    stem = pdf_path.stem

    toc = _read_toc(pdf_path)

    # ── Info Panel ───────────────────────────────────────────────
    info_lines = [
        f"📄 [cyan bold]{pdf_path.name}[/cyan bold] ({_format_size(pdf_size)})",
        f"📄 [yellow]{total_pages}[/yellow] pages",
    ]
    if toc:
        info_lines.append(f"📑 [yellow]{len(toc)}[/yellow] bookmarks detected")
    else:
        info_lines.append("[dim]📑 No bookmarks found[/dim]")

    console.print(Panel("\n".join(info_lines), title="[bold]PDF Split[/bold]", border_style="blue"))

    # ── Choose split method ──────────────────────────────────────
    if pages_per_chunk:
        method = "pages"
        n_chunks = (total_pages + pages_per_chunk - 1) // pages_per_chunk
        console.print(f"\n🔪 Splitting every [yellow]{pages_per_chunk}[/yellow] pages → "
                      f"[yellow]{n_chunks}[/yellow] parts")
    else:
        options = []
        if toc:
            options.append(f"A) By bookmarks ({len(toc)} chapters)")
        options.append("B) By page count")

        idx = select_menu("Split method:", options, show_back=True)
        if idx is None:
            return  # back to main menu

        if toc and idx == 0:
            method = "toc"
        else:
            method = "pages"
            console.print()
            try:
                pages_per_chunk = int(input("  Pages per chunk [50]: ").strip() or "50")
            except (ValueError, EOFError):
                pages_per_chunk = 50
            n_chunks = (total_pages + pages_per_chunk - 1) // pages_per_chunk
            console.print(f"  → [yellow]{n_chunks}[/yellow] parts")

    # ── Output directory ─────────────────────────────────────────
    out_dir = PDF_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Execute split ────────────────────────────────────────────
    console.print(f"\n🔪 Splitting...")

    if method == "toc":
        parts = _split_pdf_by_toc(pdf_path, toc, out_dir)
    else:
        parts = _split_pdf_by_pages(pdf_path, pages_per_chunk, out_dir)

    # ── Summary ──────────────────────────────────────────────────
    total_split_size = sum(p["pdf_path"].stat().st_size for p in parts)
    console.print(f"\n[green bold]✅ Split into {len(parts)} parts[/green bold]  "
                  f"[dim]({_format_size(total_split_size)} total)[/dim]")

    table = Table(show_header=True, header_style="bold", border_style="dim")
    table.add_column("#", justify="right", style="dim")
    table.add_column("File", style="cyan")
    table.add_column("Pages", justify="right", style="yellow")
    table.add_column("Range", justify="center", style="dim")
    table.add_column("Size", justify="right")

    for i, p in enumerate(parts, 1):
        table.add_row(
            str(i),
            p["pdf_path"].name,
            str(p["pages"]),
            f"p.{p['start']}–{p['end']}",
            _format_size(p["pdf_path"].stat().st_size),
        )

    console.print(table)

    console.print(f"\n[bold]Output directory:[/bold] [cyan]{out_dir}/[/cyan]")
    console.print(f"\n[dim]💡 To convert these PDFs: make convert[/dim]")
