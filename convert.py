#!/usr/bin/env python3
"""
PDF to Markdown converter — marker extraction + post-processing.
Pure conversion only. API enhancement is a separate step in api.py.
"""

import json
import re
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, MofNCompleteColumn, SpinnerColumn
from rich.table import Table

console = Console()

PDF_DIR = Path("pdf")
MARKDOWN_DIR = Path("markdown")

# ── Performance: override marker defaults (MPS auto-detect is broken) ──
MARKER_CONFIG = {
    "layout_batch_size": 12,
    "detection_batch_size": 8,
    "recognition_batch_size": 64,
    "equation_batch_size": 16,
    "table_rec_batch_size": 12,
    "ocr_error_batch_size": 12,
    "pdftext_workers": 1,
}

# ── Model Cache ──────────────────────────────────────────────────

_models = None


def _get_models():
    """Lazy-load marker ML models (cached after first call)."""
    global _models
    if _models is None:
        console.print("\n⏳ [bold]Loading ML models...[/bold]")
        load_start = time.time()
        from marker.models import create_model_dict
        _models = create_model_dict()
        console.print(f"  [dim]Models loaded in {time.time() - load_start:.1f}s[/dim]")
    return _models


# ── OCR Detection ────────────────────────────────────────────────

def _detect_needs_ocr(pdf_path: Path, sample_pages: int = 5) -> tuple[bool, float]:
    """Sample a few pages to check if PDF has extractable text.

    Returns (needs_ocr, avg_chars_per_page).
    If avg chars < 50 per page → probably scanned → needs OCR.
    """
    import pypdfium2
    doc = pypdfium2.PdfDocument(str(pdf_path))
    total = len(doc)
    # Sample evenly spaced pages (skip first page which might be a cover)
    indices = [min(i, total - 1) for i in range(1, sample_pages + 1)]
    indices = list(dict.fromkeys(indices))  # deduplicate

    total_chars = 0
    for i in indices:
        page = doc[i]
        tp = page.get_textpage()
        text = tp.get_text_bounded()
        total_chars += len(text.strip())
    doc.close()

    avg = total_chars / max(len(indices), 1)
    return avg < 50, avg


def ask_ocr_mode(pdf_path: Path) -> bool:
    """Ask user for OCR mode. Returns disable_ocr (True = skip OCR).

    Options:
      - Auto-detect: sample PDF, decide automatically
      - Off: always skip OCR (fastest for text PDFs)
    """
    options = [
        "Auto-detect (sample pages to decide)",
        "Off — skip OCR (fastest for text PDFs)",
    ]
    idx = select_menu("OCR mode:", options)
    if idx is None:
        # Default: auto-detect
        idx = 0

    if idx == 1:
        # Force off
        console.print("  🔇 OCR [bold]disabled[/bold] (direct text extraction)")
        return True

    # Auto-detect
    console.print("  🔍 Sampling pages...", end=" ")
    needs_ocr, avg_chars = _detect_needs_ocr(pdf_path)

    if needs_ocr:
        console.print(f"[yellow]⚠ Low text ({avg_chars:.0f} chars/page) → OCR [bold]enabled[/bold] (scanned PDF)[/yellow]")
        return False
    else:
        console.print(f"[green]✓ Text detected ({avg_chars:.0f} chars/page) → OCR [bold]disabled[/bold][/green]")
        return True


# ── Helpers ──────────────────────────────────────────────────────

def _format_size(nbytes: int) -> str:
    """Human-readable file size."""
    if nbytes < 1024:
        return f"{nbytes} B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    else:
        return f"{nbytes / (1024 * 1024):.1f} MB"


def _parse_meta(meta_path: Path) -> dict:
    """Extract page count and image count from marker's _meta.json."""
    info = {"pages": 0, "images": 0}
    if not meta_path.exists():
        return info
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        page_stats = data.get("page_stats", [])
        info["pages"] = len(page_stats)
        for page in page_stats:
            for block_type, count in page.get("block_counts", []):
                if block_type == "Picture":
                    info["images"] += count
    except Exception:
        pass
    return info


def _quick_page_count(pdf_path: Path) -> int:
    """Fast page count using pypdfium2 (no OCR, milliseconds)."""
    try:
        import pypdfium2
        doc = pypdfium2.PdfDocument(str(pdf_path))
        pages = len(doc)
        doc.close()
        return pages
    except Exception:
        return 0


def _scan_pdfs(pdf_paths: list[Path]) -> dict:
    """Scan PDFs for page counts and sizes."""
    total_pages = 0
    total_size = 0
    per_file = {}
    for p in pdf_paths:
        pages = _quick_page_count(p)
        per_file[p] = pages
        total_pages += pages
        total_size += p.stat().st_size

    return {
        "per_file": per_file,
        "total_pages": total_pages,
        "total_size": total_size,
    }


# ── Interactive Menu ─────────────────────────────────────────────

def select_menu(title: str, options: list[str]) -> int | None:
    """Arrow-key menu selection. Returns index or None if cancelled."""
    from simple_term_menu import TerminalMenu
    menu = TerminalMenu(options, title=title)
    idx = menu.show()
    return idx


# ── Post-processing ──────────────────────────────────────────────

_CPP_SIGNALS = re.compile(
    r'#include\s*[<"]|using\s+namespace|cout\s*<<|cin\s*>>|int\s+main\s*\(|'
    r'\bclass\s+\w+|template\s*<|std::|nullptr|void\s+\w+\s*\(|'
    r'\bnew\s+\w+|delete\s+\w+|->|\w+::\w+|public:|private:|protected:|'
    r'\bint\s+\w+\s*[=;{(]|\bfloat\s+\w+|double\s+\w+|bool\s+\w+|'
    r'const\s+\w+|return\s+\d|enum\s+\w+|struct\s+\w+'
)

_MAKE_SIGNALS = re.compile(
    r'^\t(g\+\+|gcc|make|rm |echo )|'
    r'^[A-Za-z_]+\s*[:+]?=|'
    r'^\.\w+\.\w+:|'
    r'\$[\(\{@<]',
    re.MULTILINE
)


def _is_cpp_code(block: str) -> bool:
    return bool(_CPP_SIGNALS.search(block))


def _is_makefile_code(block: str) -> bool:
    return bool(_MAKE_SIGNALS.search(block))


def _strip_line_numbers(code: str) -> str:
    """Remove leading line numbers from code lines."""
    lines = code.split('\n')
    numbered = 0
    total = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        total += 1
        if re.match(r'^\d{1,4}(\s|$)', stripped):
            numbered += 1

    if total == 0 or numbered / total < 0.5:
        return code

    result = []
    for line in lines:
        if re.match(r'^\s*\d{1,4}\s*$', line):
            result.append('')
            continue
        cleaned = re.sub(r'^\s*\d{1,4} ', '', line, count=1)
        result.append(cleaned)
    return '\n'.join(result)


def _normalize_headings(text: str) -> str:
    """Normalize heading levels to match reference style."""
    lines = text.split('\n')
    result = []
    in_code_block = False
    prev_was_part = False

    for line in lines:
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            result.append(line)
            prev_was_part = False
            continue

        if in_code_block:
            result.append(line)
            continue

        heading_match = re.match(r'^(#{1,6})\s+(.*)', line)
        if not heading_match:
            if line.strip():
                prev_was_part = False
            result.append(line)
            continue

        content = heading_match.group(2)

        if re.match(r'COMP\s*2012', content):
            result.append(f'## {content}')
            prev_was_part = False
        elif re.match(r'Part\s+[IVXLC\d]+', content, re.IGNORECASE):
            result.append(f'# {content}')
            prev_was_part = True
        elif prev_was_part:
            result.append(f'## {content}')
            prev_was_part = False
        else:
            result.append(f'# {content}')
            prev_was_part = False

    return '\n'.join(result)


def postprocess(text: str) -> tuple[str, dict]:
    """Clean up marker output. Returns (cleaned_text, stats)."""
    stats = {"spans_removed": 0, "links_unwrapped": 0, "sups_cleaned": 0,
             "code_blocks": 0, "code_cpp": 0, "code_makefile": 0, "code_other": 0,
             "headings": 0}

    stats["spans_removed"] = len(re.findall(r'<span id="page-\d+-\d+"></span>', text))
    stats["links_unwrapped"] = len(re.findall(r'\[([^\]]+)\]\(#page-\d+-\d+\)', text))
    stats["sups_cleaned"] = len(re.findall(r'<sup>(\d+)</sup>', text))

    text = re.sub(r'<span id="page-\d+-\d+"></span>\s*', '', text)
    text = re.sub(r'\[([^\]]+)\]\(#page-\d+-\d+\)', r'\1', text)
    text = re.sub(r'<sup>(\d+)</sup>\s*', r'\1. ', text)

    def _process_code_block(match):
        lang = match.group(1) or ''
        code = match.group(2)
        code = _strip_line_numbers(code)
        if not lang:
            if _is_cpp_code(code):
                lang = 'cpp'
            elif _is_makefile_code(code):
                lang = 'makefile'
        stats["code_blocks"] += 1
        if lang == 'cpp':
            stats["code_cpp"] += 1
        elif lang == 'makefile':
            stats["code_makefile"] += 1
        elif lang:
            stats["code_other"] += 1
        return f'```{lang}\n{code}\n```'

    text = re.sub(r'```(\w*)\n(.*?)\n```', _process_code_block, text, flags=re.DOTALL)
    text = _normalize_headings(text)
    stats["headings"] = len(re.findall(r'^#{1,6}\s', text, re.MULTILINE))
    text = re.sub(r'\n{3,}', '\n\n', text)
    lines = [line.rstrip() for line in text.split('\n')]
    text = '\n'.join(lines)
    return text, stats


# ── Conversion (marker library + postprocess, NO API) ───────────

def _make_config(disable_ocr: bool = True) -> dict:
    """Build marker config with OCR setting."""
    return {**MARKER_CONFIG, "disable_ocr": disable_ocr}


def convert_single(
    pdf_path: Path,
    output_dir: Path,
    index: int | None = None,
    total: int | None = None,
    disable_ocr: bool = True,
) -> dict:
    """Convert a single PDF to Markdown (marker + postprocess). Returns stats dict."""
    from marker.converters.pdf import PdfConverter
    from marker.output import save_output

    pdf_size = pdf_path.stat().st_size
    prefix = f"[{index}/{total}]" if index and total else ""

    console.print(f"\n[cyan bold]{prefix} {pdf_path.name}[/cyan bold] [dim]({_format_size(pdf_size)})[/dim]")
    start = time.time()

    # Step 1: marker extraction (library API — shows tqdm per page)
    models = _get_models()
    config = _make_config(disable_ocr=disable_ocr)
    converter = PdfConverter(artifact_dict=models, config=config)
    rendered = converter(str(pdf_path))

    stem = pdf_path.stem
    part_out_dir = output_dir / stem
    part_out_dir.mkdir(parents=True, exist_ok=True)
    save_output(rendered, str(part_out_dir), stem)

    md_path = part_out_dir / f"{stem}.md"
    meta_path = part_out_dir / f"{stem}_meta.json"

    marker_time = time.time() - start

    # Parse marker metadata
    meta = _parse_meta(meta_path)
    image_files = list(part_out_dir.glob("*.jpeg")) + list(part_out_dir.glob("*.png"))
    meta["images"] = len(image_files)

    console.print(f"  ├ [green]marker:[/green]  [yellow]{meta['pages']}[/yellow] pages, "
                  f"[yellow]{meta['images']}[/yellow] images  "
                  f"[dim]{marker_time:.1f}s[/dim]")

    # Step 2: local post-processing
    raw_text = md_path.read_text(encoding='utf-8')
    text, pp_stats = postprocess(raw_text)
    md_path.write_text(text, encoding='utf-8')

    # Format code block info
    code_parts = []
    if pp_stats["code_cpp"]:
        code_parts.append(f"{pp_stats['code_cpp']} cpp")
    if pp_stats["code_makefile"]:
        code_parts.append(f"{pp_stats['code_makefile']} makefile")
    if pp_stats["code_other"]:
        code_parts.append(f"{pp_stats['code_other']} other")
    code_detail = f" ({', '.join(code_parts)})" if code_parts else ""

    cleaned_count = pp_stats["spans_removed"] + pp_stats["links_unwrapped"] + pp_stats["sups_cleaned"]
    console.print(f"  ├ [green]cleanup:[/green] [yellow]{pp_stats['code_blocks']}[/yellow] code blocks{code_detail}, "
                  f"[yellow]{pp_stats['headings']}[/yellow] headings, "
                  f"[yellow]{cleaned_count}[/yellow] tags cleaned")

    # Final output info
    out_size = md_path.stat().st_size
    out_lines = len(text.splitlines())
    elapsed = time.time() - start

    console.print(f"  └ [green]output:[/green]  [yellow]{out_lines}[/yellow] lines, "
                  f"[yellow]{_format_size(out_size)}[/yellow]  "
                  f"→ [dim]{md_path}[/dim]  [dim]{elapsed:.1f}s[/dim]")

    return {
        "name": pdf_path.name,
        "status": "ok",
        "md_path": str(md_path),
        "pages": meta["pages"],
        "images": meta["images"],
        "code_blocks": pp_stats["code_blocks"],
        "headings": pp_stats["headings"],
        "out_size": out_size,
        "out_lines": out_lines,
        "time": elapsed,
    }


# ── Summary ──────────────────────────────────────────────────────

def print_summary(results: list[dict], total_elapsed: float):
    """Print a rich summary table for conversion results."""
    ok = [r for r in results if r.get("status") == "ok"]
    fail = [r for r in results if r.get("status") != "ok"]

    total_size = sum(r.get("out_size", 0) for r in ok)
    total_pages = sum(r.get("pages", 0) for r in ok)

    status_text = f"[green bold]✅ {len(ok)}/{len(results)} succeeded[/green bold]"
    if fail:
        status_text += f"  [red bold]❌ {len(fail)} failed[/red bold]"

    panel_lines = [
        status_text + f"   [dim]⏱ {total_elapsed:.0f}s total (avg {total_elapsed/max(len(results),1):.0f}s/file)[/dim]",
        f"[dim]📄 {total_pages} pages   💾 {_format_size(total_size)}[/dim]",
    ]

    console.print()
    console.print(Panel("\n".join(panel_lines), title="[bold]Summary[/bold]", border_style="blue"))

    table = Table(show_header=True, header_style="bold", border_style="dim")
    table.add_column("File", style="cyan", min_width=28)
    table.add_column("Pages", justify="right", style="yellow")
    table.add_column("Images", justify="right", style="yellow")
    table.add_column("Code", justify="right", style="yellow")
    table.add_column("Size", justify="right")
    table.add_column("Time", justify="right", style="dim")

    for r in results:
        if r.get("status") == "ok":
            table.add_row(
                r["name"], str(r["pages"]), str(r["images"]),
                str(r["code_blocks"]), _format_size(r["out_size"]),
                f"{r['time']:.0f}s",
            )
        else:
            table.add_row(f"[red]{r['name']}[/red]", "—", "—", "—", "—", "[red]FAIL[/red]")

    if len(ok) > 1:
        table.add_section()
        table.add_row(
            "[bold]TOTAL[/bold]",
            f"[bold]{total_pages}[/bold]",
            f"[bold]{sum(r.get('images', 0) for r in ok)}[/bold]",
            f"[bold]{sum(r.get('code_blocks', 0) for r in ok)}[/bold]",
            f"[bold]{_format_size(total_size)}[/bold]",
            f"[bold]{total_elapsed:.0f}s[/bold]",
        )

    console.print(table)



# ── PDF Discovery (supports split subdirectories) ────────────────

def find_pdfs(pdf_dir: Path) -> list[tuple[Path, Path]]:
    """Find all PDFs to convert, with their output directories.

    Returns list of (pdf_path, output_dir) tuples.

    Logic:
    - pdf/X.pdf → output to markdown/ (becomes markdown/X/X.md)
    - pdf/Y/Z.pdf → output to markdown/Y/ (becomes markdown/Y/Z/Z.md)
    - If pdf/X.pdf has a matching pdf/X/ with PDFs, skip pdf/X.pdf (already split)
    """
    results = []

    # Top-level PDFs
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        sub_dir = pdf_dir / pdf.stem
        if sub_dir.is_dir() and list(sub_dir.glob("*.pdf")):
            # This PDF has been split — skip it, convert parts instead
            continue
        results.append((pdf, MARKDOWN_DIR))

    # Subdirectory PDFs (from split)
    for sub in sorted(pdf_dir.iterdir()):
        if sub.is_dir() and not sub.name.startswith('.'):
            sub_pdfs = sorted(sub.glob("*.pdf"))
            if sub_pdfs:
                out = MARKDOWN_DIR / sub.name
                for pdf in sub_pdfs:
                    results.append((pdf, out))

    return results


def get_output_dir(pdf_path: Path) -> Path:
    """Determine the markdown output directory for a given PDF.

    - pdf/X.pdf → markdown/
    - pdf/Y/Z.pdf → markdown/Y/
    """
    try:
        rel = pdf_path.relative_to(PDF_DIR)
        parent = rel.parent
        if parent != Path('.'):
            return MARKDOWN_DIR / parent
    except ValueError:
        pass
    return MARKDOWN_DIR


# ── Batch Conversion ─────────────────────────────────────────────

def convert_all(pdf_dir: Path, output_dir: Path = None, disable_ocr: bool = True):
    """Convert all PDFs in pdf_dir (including split subdirectories).

    Uses find_pdfs() to discover PDFs and determine output directories.
    If pdf/X/ exists with PDFs, pdf/X.pdf is skipped (already split).
    """
    pdf_items = find_pdfs(pdf_dir)
    if not pdf_items:
        console.print(f"[red]No PDFs found in {pdf_dir}/[/red]")
        return

    total_size = sum(p.stat().st_size for p, _ in pdf_items)

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
        task = progress.add_task(
            f"[bold]Converting {len(pdf_items)} PDFs[/bold] [dim]({_format_size(total_size)})[/dim]",
            total=len(pdf_items),
        )

        for i, (pdf, out_dir) in enumerate(pdf_items, 1):
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                progress.stop()
                stats = convert_single(pdf, out_dir, index=i, total=len(pdf_items), disable_ocr=disable_ocr)
                results.append(stats)
                progress.start()
                progress.advance(task)
            except Exception as e:
                results.append({"name": pdf.name, "status": "error", "error": str(e)})
                progress.start()
                progress.advance(task)

    total_elapsed = time.time() - total_start
    print_summary(results, total_elapsed)


# ── Standalone postprocess for Makefile target ───────────────────

def postprocess_file(md_path: Path) -> None:
    """Re-run postprocess on an existing markdown file."""
    text = md_path.read_text(encoding="utf-8")
    text, _stats = postprocess(text)
    md_path.write_text(text, encoding="utf-8")
