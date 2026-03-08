#!/usr/bin/env python3
"""
Split a large PDF by chapters and convert each to Markdown.

Supports three split methods:
  A) By bookmarks (PDF TOC)
  B) By page count (e.g. every 50 pages)
  C) Convert whole PDF, then split MD by # headings

Usage:
    python split.py textbook.pdf              # Auto-detect bookmarks
    python split.py textbook.pdf --pages 50   # Split every 50 pages
"""

import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pypdfium2
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, MofNCompleteColumn, SpinnerColumn
from rich.panel import Panel

from convert import (
    console,
    convert_single,
    postprocess,
    interactive_select,
    select_menu,
    print_summary,
    _format_size,
    _quick_page_count,
    TOKENS_PER_PAGE_B,
    TOKENS_PER_PAGE_C,
    MODES,
    OUTPUT_DIR,
)
from api import call_api_chunked, empty_usage, enhance_file

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

    # Top-level bookmarks only
    min_level = min(it["level"] for it in raw_items)
    chapters = [it for it in raw_items if it["level"] == min_level]

    for i, ch in enumerate(chapters):
        if i + 1 < len(chapters):
            ch["end_page"] = chapters[i + 1]["start_page"] - 1
        else:
            ch["end_page"] = total_pages - 1
        ch["num_pages"] = ch["end_page"] - ch["start_page"] + 1

    return chapters


def _slugify(text: str) -> str:
    """Convert title to filesystem-friendly slug."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text)
    return text[:60].strip('-') or "untitled"


# ── PDF Splitting ────────────────────────────────────────────────

def _split_pdf_by_toc(pdf_path: Path, chapters: list[dict], tmp_dir: Path) -> list[dict]:
    """Split PDF by TOC chapters. Returns list of part dicts."""
    doc = pypdfium2.PdfDocument(str(pdf_path))
    parts = []

    for i, ch in enumerate(chapters):
        slug = _slugify(ch["title"])
        part_name = f"ch{i+1:02d}-{slug}"
        part_path = tmp_dir / f"{part_name}.pdf"

        new_doc = pypdfium2.PdfDocument.new()
        page_indices = list(range(ch["start_page"], ch["end_page"] + 1))
        new_doc.import_pages(doc, pages=page_indices)
        new_doc.save(str(part_path))
        new_doc.close()

        parts.append({
            "title": ch["title"],
            "slug": part_name,
            "pdf_path": part_path,
            "pages": ch["num_pages"],
            "start": ch["start_page"] + 1,
            "end": ch["end_page"] + 1,
        })

    doc.close()
    return parts


def _split_pdf_by_pages(pdf_path: Path, chunk_size: int, tmp_dir: Path) -> list[dict]:
    """Split PDF into chunks of chunk_size pages."""
    doc = pypdfium2.PdfDocument(str(pdf_path))
    total = len(doc)
    parts = []
    stem = pdf_path.stem

    chunk_idx = 0
    for start in range(0, total, chunk_size):
        chunk_idx += 1
        end = min(start + chunk_size - 1, total - 1)
        num_pages = end - start + 1

        part_name = f"{stem}-part{chunk_idx:02d}"
        part_path = tmp_dir / f"{part_name}.pdf"

        new_doc = pypdfium2.PdfDocument.new()
        page_indices = list(range(start, end + 1))
        new_doc.import_pages(doc, pages=page_indices)
        new_doc.save(str(part_path))
        new_doc.close()

        parts.append({
            "title": f"Part {chunk_idx} (pp.{start+1}-{end+1})",
            "slug": part_name,
            "pdf_path": part_path,
            "pages": num_pages,
            "start": start + 1,
            "end": end + 1,
        })

    doc.close()
    return parts


# ── MD Splitting ─────────────────────────────────────────────────

def _split_md_by_headings(md_text: str) -> list[tuple[str, str]]:
    """Split markdown by top-level # headings. Returns [(title, content), ...]."""
    sections = re.split(r'(?=^# )', md_text, flags=re.MULTILINE)
    sections = [s for s in sections if s.strip()]

    results = []
    for i, section in enumerate(sections):
        first_line = section.split('\n', 1)[0]
        title_match = re.match(r'^#\s+(.*)', first_line)
        title = title_match.group(1).strip() if title_match else f"Section {i+1}"
        results.append((title, section))

    return results


# ── Main Split + Convert Flow ────────────────────────────────────

def split_and_convert(pdf_path: Path, pages_per_chunk: int | None = None):
    """Main entry: split a PDF and convert each part to Markdown."""
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

    # ── Choose convert mode ──────────────────────────────────────
    est_b = total_pages * TOKENS_PER_PAGE_B
    est_c = total_pages * TOKENS_PER_PAGE_C
    mode, model_id, model_name = interactive_select(est_b=est_b, est_c=est_c)

    mode_desc = MODES[mode]
    mode_line = f"\n🔧 Mode [bold]{mode}[/bold] — {mode_desc}"
    if model_name:
        mode_line += f"  [cyan]{model_name}[/cyan]"
    console.print(mode_line)

    est_tokens = est_b if mode == "B" else est_c if mode == "C" else 0

    # ── Output directory ─────────────────────────────────────────
    out_dir = OUTPUT_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Execute ──────────────────────────────────────────────────
    if split_method == "headings":
        _do_heading_split(pdf_path, out_dir, stem, mode, model_id, est_tokens)
    else:
        _do_pdf_split(pdf_path, out_dir, split_method, toc, pages_per_chunk, mode, model_id, est_tokens)


def _do_pdf_split(pdf_path: Path, out_dir: Path, method: str, toc: list[dict],
                  pages_per_chunk: int | None, mode: str, model_id: str | None,
                  est_tokens: int):
    """Split PDF into parts, convert each."""
    with tempfile.TemporaryDirectory(prefix="pdf_split_") as tmp:
        tmp_dir = Path(tmp)

        if method == "toc":
            parts = _split_pdf_by_toc(pdf_path, toc, tmp_dir)
        else:
            parts = _split_pdf_by_pages(pdf_path, pages_per_chunk, tmp_dir)

        console.print(f"\n[bold]Converting {len(parts)} parts...[/bold]")

        results = []
        total_start = time.time()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=40),
            MofNCompleteColumn(),
            TextColumn("[dim]ETA[/dim]"),
            TimeRemainingColumn(),
            console=console,
            transient=False,
        ) as progress:
            task = progress.add_task(f"[bold]Converting {len(parts)} parts[/bold]", total=len(parts))

            for i, part in enumerate(parts, 1):
                try:
                    progress.stop()

                    # Convert this chunk PDF
                    stats = convert_single(part["pdf_path"], tmp_dir, index=i, total=len(parts))

                    # API enhancement (separate step)
                    if mode in ("B", "C") and model_id:
                        api_start = time.time()
                        usage = enhance_file(Path(stats["md_path"]), mode, model_id)
                        stats["api_usage"] = usage
                        stats["api_time"] = time.time() - api_start
                        stats["time"] += stats["api_time"]

                    # Move output to final location
                    chunk_stem = part["pdf_path"].stem
                    src_md = tmp_dir / chunk_stem / f"{chunk_stem}.md"
                    dst_md = out_dir / f"{part['slug']}.md"

                    if src_md.exists():
                        shutil.copy2(src_md, dst_md)
                        src_img_dir = tmp_dir / chunk_stem
                        for img in list(src_img_dir.glob("*.jpeg")) + list(src_img_dir.glob("*.png")):
                            shutil.copy2(img, out_dir / img.name)

                    stats["name"] = f"{part['slug']}.md"
                    results.append(stats)
                    progress.start()
                    progress.advance(task)

                except Exception as e:
                    results.append({"name": part["slug"], "status": "error", "error": str(e)})
                    progress.start()
                    progress.advance(task)

        total_elapsed = time.time() - total_start
        print_summary(results, total_elapsed, mode, est_tokens)

        console.print(f"\n[bold]Output directory:[/bold] [cyan]{out_dir}/[/cyan]")
        for md in sorted(out_dir.glob("*.md")):
            console.print(f"  📄 {md.name}")


def _do_heading_split(pdf_path: Path, out_dir: Path, stem: str, mode: str,
                      model_id: str | None, est_tokens: int):
    """Convert whole PDF, then split the resulting MD by headings."""
    total_start = time.time()

    # Step 1: Convert the whole PDF
    console.print(f"\n[bold]Step 1:[/bold] Converting entire PDF...")
    stats = convert_single(pdf_path, OUTPUT_DIR, index=1, total=1)

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

    # Step 3: Write sections + optional API enhancement
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
            "api_usage": empty_usage(),
            "out_size": section_path.stat().st_size,
            "out_lines": len(text.splitlines()),
            "time": 0,
            "marker_time": 0,
            "api_time": 0,
        }

        # API enhancement
        if mode in ("B", "C") and model_id:
            console.print(f"  [{i}/{len(sections)}] API: [cyan]{title[:50]}[/cyan]...")
            api_start = time.time()
            usage = enhance_file(section_path, mode, model_id)
            section_stats["api_usage"] = usage
            section_stats["api_time"] = time.time() - api_start
            section_stats["out_size"] = section_path.stat().st_size

        results.append(section_stats)
        console.print(f"  ✓ [dim]{section_path.name}[/dim] ({_format_size(section_stats['out_size'])})")

    total_elapsed = time.time() - total_start
    print_summary(results, total_elapsed, mode, est_tokens)

    console.print(f"\n[bold]Output directory:[/bold] [cyan]{out_dir}/[/cyan]")
    for md in sorted(out_dir.glob("*.md")):
        console.print(f"  📄 {md.name}")


