#!/usr/bin/env python3
"""
Split a large PDF by chapters and convert each to Markdown.
Pure conversion only — API enhancement is a separate step.

Supports three split methods:
  A) By bookmarks (PDF TOC)
  B) By page count (e.g. every 50 pages)
  C) Convert whole PDF, then split MD by # headings

Usage (via main.py):
    python main.py split textbook.pdf              # Auto-detect bookmarks
    python main.py split textbook.pdf --pages 50   # Split every 50 pages
"""

import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pypdfium2
from rich.panel import Panel

from convert import (
    console,
    convert_single,
    postprocess,
    select_menu,
    print_summary,
    ask_ocr_mode,
    _format_size,
    _quick_page_count,
    _get_models,
    _make_config,
    MARKDOWN_DIR,
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
            "slug": chunk_name,
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
            "title": f"Pages {start+1}–{end}",
            "slug": chunk_name,
            "pages": end - start,
            "start": start + 1,
            "end": end,
        })

    doc.close()
    return results


# ── Markdown Splitting ───────────────────────────────────────────

def _split_md_by_headings(md_text: str) -> list[tuple[str, str]]:
    """Split markdown by top-level headings (# ...). Returns [(title, content), ...]."""
    lines = md_text.split("\n")
    sections = []
    current_title = None
    current_lines = []

    for line in lines:
        if re.match(r"^# [^#]", line):
            if current_title is not None:
                sections.append((current_title, "\n".join(current_lines)))
            current_title = line.lstrip("# ").strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_title is not None:
        sections.append((current_title, "\n".join(current_lines)))

    return sections


# ── Main Split + Convert Flow ────────────────────────────────────

def split_and_convert(pdf_path: Path, pages_per_chunk: int | None = None):
    """Main entry: split a PDF and convert each part to Markdown (no API)."""
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

    console.print(Panel("\n".join(info_lines), title="[bold]PDF Split → Markdown[/bold]", border_style="blue"))

    # ── OCR mode ─────────────────────────────────────────────────
    disable_ocr = ask_ocr_mode(pdf_path)

    # ── Choose split method ──────────────────────────────────────
    if pages_per_chunk:
        split_method = "pages"
        n_chunks = (total_pages + pages_per_chunk - 1) // pages_per_chunk
        console.print(f"\n🔪 Splitting every [yellow]{pages_per_chunk}[/yellow] pages → "
                      f"[yellow]{n_chunks}[/yellow] parts")
    else:
        options = []
        if toc:
            options.append(f"A) By bookmarks ({len(toc)} chapters)")
        options.append("B) By page count")
        options.append("C) Convert whole, split MD by headings")

        idx = select_menu("Split method:", options)
        if idx is None:
            console.print("[dim]Cancelled.[/dim]")
            sys.exit(0)

        if toc and idx == 0:
            split_method = "toc"
        elif (toc and idx == 1) or (not toc and idx == 0):
            split_method = "pages"
            console.print()
            try:
                pages_per_chunk = int(input("  Pages per chunk [50]: ").strip() or "50")
            except (ValueError, EOFError):
                pages_per_chunk = 50
            n_chunks = (total_pages + pages_per_chunk - 1) // pages_per_chunk
            console.print(f"  → [yellow]{n_chunks}[/yellow] parts")
        else:
            split_method = "headings"

    # ── Output directory ─────────────────────────────────────────
    out_dir = MARKDOWN_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Execute ──────────────────────────────────────────────────
    if split_method == "headings":
        _do_heading_split(pdf_path, out_dir, stem, disable_ocr=disable_ocr)
    else:
        _do_pdf_split(pdf_path, out_dir, split_method, toc, pages_per_chunk, disable_ocr=disable_ocr)


def _do_pdf_split(pdf_path: Path, out_dir: Path, method: str, toc: list[dict],
                  pages_per_chunk: int | None, disable_ocr: bool = True):
    """Split PDF into parts, convert each with marker library (page-level tqdm), postprocess."""
    from marker.converters.pdf import PdfConverter
    from marker.output import save_output

    with tempfile.TemporaryDirectory(prefix="pdf_split_") as tmp:
        tmp_dir = Path(tmp)

        # Step 1: Split PDF into parts
        input_dir = tmp_dir / "_input"
        input_dir.mkdir()

        if method == "toc":
            parts = _split_pdf_by_toc(pdf_path, toc, input_dir)
        else:
            parts = _split_pdf_by_pages(pdf_path, pages_per_chunk, input_dir)

        total_pages = sum(p["pages"] for p in parts)
        console.print(f"\n[bold]Converting {len(parts)} parts ({total_pages} pages)...[/bold]")

        # Step 2: Load models once
        models = _get_models()
        config = _make_config(disable_ocr=disable_ocr)

        # Step 3: Convert each part (tqdm shows per-page progress)
        total_start = time.time()
        results = []

        for i, part in enumerate(parts, 1):
            part_start = time.time()
            part_stem = part["pdf_path"].stem

            console.print(f"\n[cyan bold][{i}/{len(parts)}] {part['slug']}[/cyan bold] "
                          f"[dim]({part['pages']} pages)[/dim]")

            # Convert with marker library (tqdm bars visible)
            converter = PdfConverter(artifact_dict=models, config=config)
            rendered = converter(str(part["pdf_path"]))

            # Save to temp location
            part_out_dir = tmp_dir / part_stem
            part_out_dir.mkdir(parents=True, exist_ok=True)
            save_output(rendered, str(part_out_dir), part_stem)

            part_time = time.time() - part_start

            # Read and postprocess
            src_md = part_out_dir / f"{part_stem}.md"
            if not src_md.exists():
                md_files = list(part_out_dir.glob("*.md"))
                if md_files:
                    src_md = md_files[0]
                else:
                    console.print(f"  [red]✗ No output for {part['slug']}[/red]")
                    results.append({"name": part["slug"], "status": "error", "error": "no output"})
                    continue

            text = src_md.read_text(encoding="utf-8")
            text, pp_stats = postprocess(text)

            # Read meta for image count
            meta_path = part_out_dir / f"{part_stem}_meta.json"
            pages = part["pages"]
            images = 0
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                    images = len(meta.get("images", {}))
                except Exception:
                    pass

            # Write to final location
            dst_md = out_dir / f"{part['slug']}.md"
            dst_md.write_text(text, encoding="utf-8")

            # Copy images
            for img in list(part_out_dir.glob("*.jpeg")) + list(part_out_dir.glob("*.png")):
                shutil.copy2(img, out_dir / img.name)

            stats = {
                "name": f"{part['slug']}.md",
                "status": "ok",
                "md_path": str(dst_md),
                "pages": pages,
                "images": images,
                "code_blocks": pp_stats["code_blocks"],
                "headings": pp_stats["headings"],
                "out_size": dst_md.stat().st_size,
                "out_lines": len(text.splitlines()),
                "time": part_time,
            }

            console.print(
                f"  ✓ {pages}p, {_format_size(stats['out_size'])},"
                f" {stats['out_lines']} lines,"
                f" {pp_stats['headings']}h, {images} img"
                f"  [dim]{part_time:.1f}s[/dim]"
            )
            results.append(stats)

        total_elapsed = time.time() - total_start
        print_summary(results, total_elapsed)

        console.print(f"\n[bold]Output directory:[/bold] [cyan]{out_dir}/[/cyan]")
        for md in sorted(out_dir.glob("*.md")):
            console.print(f"  📄 {md.name}")
        console.print(f"\n[dim]💡 To enhance with API: make enhance[/dim]")


def _do_heading_split(pdf_path: Path, out_dir: Path, stem: str, disable_ocr: bool = True):
    """Convert whole PDF, then split the resulting MD by headings."""
    total_start = time.time()

    # Step 1: Convert the whole PDF
    console.print(f"\n[bold]Step 1:[/bold] Converting entire PDF...")
    stats = convert_single(pdf_path, MARKDOWN_DIR, index=1, total=1, disable_ocr=disable_ocr)

    whole_md_path = Path(stats["md_path"])
    md_text = whole_md_path.read_text(encoding="utf-8")

    # Step 2: Split by headings
    console.print(f"\n[bold]Step 2:[/bold] Splitting by [cyan]#[/cyan] headings...")
    sections = _split_md_by_headings(md_text)

    if len(sections) <= 1:
        console.print("[yellow]Only 1 section found — nothing to split.[/yellow]")
        console.print(f"  → Output remains at [dim]{whole_md_path}[/dim]")
        return

    console.print(f"  Found [yellow]{len(sections)}[/yellow] sections")

    # Step 3: Write sections
    results = []
    for i, (title, content) in enumerate(sections, 1):
        slug = _slugify(title)
        section_name = f"{stem}-{i:02d}-{slug}"
        section_path = out_dir / f"{section_name}.md"

        text, pp_stats = postprocess(content)
        section_path.write_text(text, encoding="utf-8")

        section_stats = {
            "name": f"{section_name}.md",
            "status": "ok",
            "md_path": str(section_path),
            "pages": 0,
            "images": 0,
            "code_blocks": pp_stats["code_blocks"],
            "headings": pp_stats["headings"],
            "out_size": section_path.stat().st_size,
            "out_lines": len(text.splitlines()),
            "time": 0,
        }

        results.append(section_stats)
        console.print(f"  ✓ [dim]{section_path.name}[/dim] ({_format_size(section_stats['out_size'])})")

    total_elapsed = time.time() - total_start
    print_summary(results, total_elapsed)

    console.print(f"\n[bold]Output directory:[/bold] [cyan]{out_dir}/[/cyan]")
    for md in sorted(out_dir.glob("*.md")):
        console.print(f"  📄 {md.name}")
    console.print(f"\n[dim]💡 To enhance with API: make enhance[/dim]")
