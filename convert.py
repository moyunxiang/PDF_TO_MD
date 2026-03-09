#!/usr/bin/env python3
"""
PDF to Markdown converter — marker extraction + post-processing.
Pure conversion only. API enhancement is a separate step in api.py.
"""

import json
import re
import subprocess
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, MofNCompleteColumn, SpinnerColumn
from rich.table import Table

console = Console()

PDF_DIR = Path("pdf")
MARKDOWN_DIR = Path("markdown")
MARKER_BIN = Path(".venv/bin/marker_single")
MARKER_BATCH_BIN = Path(".venv/bin/marker")

# ── Performance: override marker defaults (MPS auto-detect is broken) ──
MARKER_PERF_ARGS = [
    "--layout_batch_size", "12",
    "--detection_batch_size", "8",
    "--recognition_batch_size", "64",
    "--equation_batch_size", "16",
    "--table_rec_batch_size", "12",
    "--ocr_error_batch_size", "12",
]

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


# ── Conversion (marker + postprocess only, NO API) ──────────────

def convert_single(
    pdf_path: Path,
    output_dir: Path,
    index: int | None = None,
    total: int | None = None,
) -> dict:
    """Convert a single PDF to Markdown (marker + postprocess). Returns stats dict."""
    pdf_size = pdf_path.stat().st_size
    prefix = f"[{index}/{total}]" if index and total else ""

    console.print(f"\n[cyan bold]{prefix} {pdf_path.name}[/cyan bold] [dim]({_format_size(pdf_size)})[/dim]")
    start = time.time()

    # Step 1: marker extraction
    result = subprocess.run(
        [str(MARKER_BIN), str(pdf_path), "--output_dir", str(output_dir), *MARKER_PERF_ARGS],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"  [red bold]✗ marker error:[/red bold]\n[red]{result.stderr[:500]}[/red]")
        raise RuntimeError(f"marker failed for {pdf_path.name}")

    stem = pdf_path.stem
    md_path = output_dir / stem / f"{stem}.md"
    meta_path = output_dir / stem / f"{stem}_meta.json"

    marker_time = time.time() - start

    # Parse marker metadata
    meta = _parse_meta(meta_path)
    image_files = list((output_dir / stem).glob("*.jpeg")) + list((output_dir / stem).glob("*.png"))
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


# ── Batch Conversion ─────────────────────────────────────────────

def convert_all(pdf_dir: Path, output_dir: Path):
    """Convert all PDFs in pdf_dir. Pure marker conversion, no API."""
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        console.print(f"[red]No PDFs found in {pdf_dir}/[/red]")
        return

    total_size = sum(p.stat().st_size for p in pdfs)
    output_dir.mkdir(parents=True, exist_ok=True)

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
            f"[bold]Converting {len(pdfs)} PDFs[/bold] [dim]({_format_size(total_size)})[/dim]",
            total=len(pdfs),
        )

        for i, pdf in enumerate(pdfs, 1):
            try:
                progress.stop()
                stats = convert_single(pdf, output_dir, index=i, total=len(pdfs))
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
