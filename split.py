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
    python main.py split textbook.pdf --workers 3  # Parallel workers
"""

import json
import os
import re
import shutil
import subprocess
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
    select_menu,
    print_summary,
    _format_size,
    _quick_page_count,
    OUTPUT_DIR,
    MARKER_BATCH_BIN,
    MARKER_PERF_ARGS,
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


# ── Hardware Detection ───────────────────────────────────────────

def _detect_hardware() -> dict:
    """Detect CPU cores and total memory for worker recommendation."""
    cpu_count = os.cpu_count() or 4

    # Try to get total memory
    total_mem_gb = 8  # fallback
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            total_mem_gb = int(result.stdout.strip()) // (1024 ** 3)
    except Exception:
        pass

    # Try to get CPU brand
    cpu_brand = "Unknown"
    try:
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            cpu_brand = result.stdout.strip()
    except Exception:
        pass

    # Each marker worker uses ~8 GB peak (5 ML models + PyTorch + tensors)
    # Keep ~4 GB free for OS + apps
    mem_per_worker = 8
    usable_mem = max(0, total_mem_gb - 4)
    max_by_mem = max(1, usable_mem // mem_per_worker)
    max_by_cpu = max(1, cpu_count // 4)
    recommended = max(1, min(max_by_mem, max_by_cpu))

    return {
        "cpu_count": cpu_count,
        "cpu_brand": cpu_brand,
        "total_mem_gb": total_mem_gb,
        "recommended_workers": recommended,
        "max_safe_workers": max_by_mem,
    }


def _ask_workers(n_parts: int, workers_override: int | None = None) -> int:
    """Ask user for worker count, or use override if provided."""
    hw = _detect_hardware()

    # If CLI override, use it (but warn if too high)
    if workers_override is not None:
        if workers_override > hw["max_safe_workers"]:
            console.print(
                f"[yellow]⚠️  {workers_override} workers may cause memory swapping "
                f"on {hw['total_mem_gb']}GB RAM (each worker uses ~8GB)[/yellow]"
            )
        return max(1, workers_override)

    # Show hardware info panel
    rec = hw["recommended_workers"]
    max_safe = hw["max_safe_workers"]
    info_lines = [
        f"🖥  [bold]{hw['cpu_brand']}[/bold] · {hw['cpu_count']} cores · {hw['total_mem_gb']} GB",
        f"📦 [yellow]{n_parts}[/yellow] parts to convert (~8 GB/worker)",
        f"⚡ Recommended: [green bold]{rec}[/green bold] workers (safe max: {max_safe})",
    ]
    console.print(Panel("\n".join(info_lines), title="[bold]Performance[/bold]", border_style="yellow"))

    # Ask user
    try:
        raw = input(f"⚡ Workers [{rec}]: ").strip()
        if not raw:
            return rec
        workers = int(raw)
        if workers < 1:
            workers = 1
        if workers > max_safe:
            console.print(
                f"[yellow]⚠️  {workers} workers may exceed {hw['total_mem_gb']}GB RAM — "
                f"each worker loads ~8GB of ML models. Proceed anyway.[/yellow]"
            )
        return workers
    except (ValueError, EOFError):
        return rec


# ── Main Split + Convert Flow ────────────────────────────────────

def split_and_convert(pdf_path: Path, pages_per_chunk: int | None = None, workers: int | None = None):
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
    out_dir = OUTPUT_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Execute ──────────────────────────────────────────────────
    if split_method == "headings":
        _do_heading_split(pdf_path, out_dir, stem)
    else:
        _do_pdf_split(pdf_path, out_dir, split_method, toc, pages_per_chunk, workers)


def _do_pdf_split(pdf_path: Path, out_dir: Path, method: str, toc: list[dict],
                  pages_per_chunk: int | None, workers_override: int | None = None):
    """Split PDF into parts, batch convert with marker, postprocess."""
    with tempfile.TemporaryDirectory(prefix="pdf_split_") as tmp:
        tmp_dir = Path(tmp)

        # Step 1: Split PDF into parts (into a clean input-only directory)
        input_dir = tmp_dir / "_input"
        input_dir.mkdir()

        if method == "toc":
            parts = _split_pdf_by_toc(pdf_path, toc, input_dir)
        else:
            parts = _split_pdf_by_pages(pdf_path, pages_per_chunk, input_dir)

        # Step 2: Ask user for workers
        n_workers = _ask_workers(len(parts), workers_override)

        console.print(f"\n[bold]Converting {len(parts)} parts with {n_workers} worker(s)...[/bold]")

        # Step 3: Batch convert with marker CLI
        batch_out_dir = tmp_dir / "_batch_out"
        batch_out_dir.mkdir()

        total_start = time.time()

        cmd = [
            str(MARKER_BATCH_BIN), str(input_dir),
            "--workers", str(n_workers),
            "--output_dir", str(batch_out_dir),
            *MARKER_PERF_ARGS,
        ]
        console.print(f"[dim]$ {' '.join(str(c) for c in cmd[:6])} ...[/dim]")

        result = subprocess.run(cmd, capture_output=True, text=True)
        marker_time = time.time() - total_start

        if result.returncode != 0:
            console.print(f"[red bold]✗ marker batch error:[/red bold]\n[red]{result.stderr[:1000]}[/red]")
            raise RuntimeError("marker batch conversion failed")

        # Print marker's throughput info if available
        for line in (result.stdout + result.stderr).splitlines():
            if "pages" in line.lower() and ("throughput" in line.lower() or "pages/sec" in line.lower()):
                console.print(f"  [dim]{line.strip()}[/dim]")
                break

        console.print(f"  ⏱  marker batch done in [bold]{marker_time:.1f}s[/bold]")

        # Step 4: Postprocess + move to final directory
        console.print(f"\n[bold]Post-processing {len(parts)} parts...[/bold]")

        results = []
        for i, part in enumerate(parts, 1):
            part_stem = part["pdf_path"].stem
            # marker outputs to batch_out_dir/stem/stem.md
            src_dir = batch_out_dir / part_stem
            src_md = src_dir / f"{part_stem}.md"

            if not src_md.exists():
                # Try to find any .md file in the directory
                md_files = list(src_dir.glob("*.md")) if src_dir.exists() else []
                if md_files:
                    src_md = md_files[0]
                else:
                    console.print(f"  [red]✗ No output for {part['slug']}[/red]")
                    results.append({"name": part["slug"], "status": "error", "error": "no output"})
                    continue

            # Read and postprocess
            text = src_md.read_text(encoding="utf-8")
            text, pp_stats = postprocess(text)

            # Read meta.json for page/image count
            meta_path = src_dir / f"{part_stem}_meta.json"
            pages = part["pages"]
            images = 0
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                    images = len(meta.get("images", {}))
                except Exception:
                    pass

            # Write postprocessed MD to final location
            dst_md = out_dir / f"{part['slug']}.md"
            dst_md.write_text(text, encoding="utf-8")

            # Copy images
            for img in list(src_dir.glob("*.jpeg")) + list(src_dir.glob("*.png")):
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
                "time": marker_time / len(parts),  # approximate per-part
            }

            console.print(
                f"  ✓ [{i}/{len(parts)}] [dim]{part['slug']}.md[/dim]"
                f"  ({pages}p, {_format_size(stats['out_size'])},"
                f" {stats['out_lines']} lines,"
                f" {pp_stats['headings']}h, {images} img)"
            )
            results.append(stats)

        total_elapsed = time.time() - total_start
        print_summary(results, total_elapsed)

        console.print(f"\n[bold]Output directory:[/bold] [cyan]{out_dir}/[/cyan]")
        for md in sorted(out_dir.glob("*.md")):
            console.print(f"  📄 {md.name}")
        console.print(f"\n[dim]💡 To enhance with API: make enhance[/dim]")


def _do_heading_split(pdf_path: Path, out_dir: Path, stem: str):
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
