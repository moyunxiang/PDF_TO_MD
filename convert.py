#!/usr/bin/env python3
"""
PDF to Markdown converter using marker-pdf, with post-processing and optional API enhancement.

Usage:
    python convert.py                    # Convert all PDFs in pdf/
    python convert.py 1.review-const.pdf # Convert a single PDF
"""

import json
import os
import re
import sys
import subprocess
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, MofNCompleteColumn, SpinnerColumn
from rich.table import Table

console = Console()

PDF_DIR = Path("pdf")
OUTPUT_DIR = Path("output")
MARKER_BIN = Path(".venv/bin/marker_single")

# ── Token estimation constants (calibrated from 13 samples) ─────
# content ≈ 130 tokens/page
# Mode B: prompt ≈ input, completion ≈ input → total ≈ 2× content
# Mode C: prompt ≈ input, completion ≈ 1.3× input → total ≈ 2.5× content
TOKENS_PER_PAGE_B = 260
TOKENS_PER_PAGE_C = 325

# ── Models ───────────────────────────────────────────────────────

MODELS = {
    "1": {"name": "gpt-4o-mini",           "id": "openai/gpt-4o-mini"},
    "2": {"name": "deepseek-v3.2",         "id": "deepseek/deepseek-chat-v3-0324"},
    "3": {"name": "qwen2.5-72b-instruct",  "id": "qwen/qwen-2.5-72b-instruct"},
}

MODES = {
    "A": "Direct output (no API)",
    "B": "API format cleanup",
    "C": "API understand + rewrite",
}

# ── Prompts ──────────────────────────────────────────────────────

PROMPT_B = """\
You are a Markdown formatting assistant. Clean up the following lecture note Markdown:

Rules:
- Fix any formatting issues (broken tables, misaligned lists, inconsistent spacing)
- Ensure code blocks have correct language tags (```cpp for C++, ```makefile for Makefile)
- Standardize heading levels: # for slide titles, ## for subtitles only
- Keep ALL content exactly as-is — do not add, remove, or rephrase any text
- Keep image references as-is
- Output ONLY the cleaned Markdown, no explanations

Markdown to clean:
"""

PROMPT_C = """\
You are an expert at creating clear, well-organized study notes from lecture slides.
Rewrite the following lecture note Markdown into a polished study guide:

Rules:
- Reorganize content for logical flow (group related topics)
- Use clear heading hierarchy: # for major topics, ## for subtopics, ### for details
- Preserve ALL technical content, code examples, and formulas accurately
- Improve clarity: add brief explanations where slides are terse
- Format code blocks with correct language tags (```cpp for C++, ```makefile for Makefile)
- Use tables, bullet points, and bold for key concepts
- Keep image references as-is
- Output ONLY the rewritten Markdown, no explanations

Lecture notes to rewrite:
"""

# ── Helpers ──────────────────────────────────────────────────────

def _format_size(nbytes: int) -> str:
    """Human-readable file size."""
    if nbytes < 1024:
        return f"{nbytes} B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    else:
        return f"{nbytes / (1024 * 1024):.1f} MB"


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English/code mix."""
    return max(1, len(text) // 4)


def _empty_usage() -> dict:
    """Return a zeroed token usage dict."""
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


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
    """Scan PDFs for page counts and token estimates. Returns scan info dict."""
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
        "est_b": total_pages * TOKENS_PER_PAGE_B,
        "est_c": total_pages * TOKENS_PER_PAGE_C,
    }


# ── Interactive Menu ─────────────────────────────────────────────

def select_menu(title: str, options: list[str]) -> int | None:
    """Arrow-key menu selection. Returns index or None if cancelled."""
    from simple_term_menu import TerminalMenu
    menu = TerminalMenu(options, title=title)
    idx = menu.show()
    return idx


def interactive_select(est_b: int = 0, est_c: int = 0) -> tuple[str, str | None, str | None]:
    """Interactive mode/model selection. Returns (mode, model_id, model_name)."""
    # Build mode options with token estimates for B/C
    mode_a = "A) Direct output (no API)"
    mode_b = f"B) API format cleanup         (~{est_b:,} tokens est.)" if est_b else "B) API format cleanup"
    mode_c = f"C) API understand + rewrite   (~{est_c:,} tokens est.)" if est_c else "C) API understand + rewrite"
    mode_options = [mode_a, mode_b, mode_c]

    idx = select_menu("Select mode:", mode_options)
    if idx is None:
        console.print("[dim]Cancelled.[/dim]")
        sys.exit(0)

    mode = list(MODES.keys())[idx]

    if mode == "A":
        return mode, None, None

    # Check API key before showing model menu
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        console.print("\n[red bold]✗ OPENROUTER_API_KEY not set.[/red bold]")
        console.print("  Export it first:  [cyan]export OPENROUTER_API_KEY=sk-or-...[/cyan]")
        sys.exit(1)

    model_options = [f"{k}) {v['name']}" for k, v in MODELS.items()]
    idx = select_menu("Select model:", model_options)
    if idx is None:
        console.print("[dim]Cancelled.[/dim]")
        sys.exit(0)

    model_key = list(MODELS.keys())[idx]
    model_id = MODELS[model_key]["id"]
    model_name = MODELS[model_key]["name"]

    return mode, model_id, model_name


# ── API Call ─────────────────────────────────────────────────────

def call_api(text: str, mode: str, model_id: str) -> tuple[str, dict]:
    """Send markdown to OpenRouter API. Returns (result_text, usage_dict)."""
    from openai import OpenAI

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
    )

    prompt = PROMPT_B if mode == "B" else PROMPT_C

    response = client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
        temperature=0.1 if mode == "B" else 0.3,
    )

    # Extract actual token usage from API response
    usage = _empty_usage()
    if response.usage:
        usage["prompt_tokens"] = getattr(response.usage, "prompt_tokens", 0) or 0
        usage["completion_tokens"] = getattr(response.usage, "completion_tokens", 0) or 0
        usage["total_tokens"] = getattr(response.usage, "total_tokens", 0) or 0

    return response.choices[0].message.content, usage


def call_api_chunked(text: str, mode: str, model_id: str, chunk_limit: int = 60000) -> tuple[str, dict]:
    """For large files, split by # headings and process each chunk. Returns (result, total_usage)."""
    if len(text) <= chunk_limit:
        return call_api(text, mode, model_id)

    # Split by top-level headings
    sections = re.split(r'(?=^# )', text, flags=re.MULTILINE)
    sections = [s for s in sections if s.strip()]

    # Merge small sections into chunks under the limit
    chunks = []
    current = ""
    for section in sections:
        if len(current) + len(section) > chunk_limit and current:
            chunks.append(current)
            current = section
        else:
            current += section
    if current:
        chunks.append(current)

    console.print(f"    [dim]split into {len(chunks)} chunks for API[/dim]")

    results = []
    total_usage = _empty_usage()
    for i, chunk in enumerate(chunks):
        console.print(f"    [dim]chunk {i+1}/{len(chunks)} ({len(chunk)//1000}K chars)...[/dim]")
        result, usage = call_api(chunk, mode, model_id)
        results.append(result)
        for k in total_usage:
            total_usage[k] += usage[k]

    return "\n\n".join(results), total_usage


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

    # Count removals
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

    # Count headings
    stats["headings"] = len(re.findall(r'^#{1,6}\s', text, re.MULTILINE))

    text = re.sub(r'\n{3,}', '\n\n', text)
    lines = [line.rstrip() for line in text.split('\n')]
    text = '\n'.join(lines)
    return text, stats


# ── Conversion ───────────────────────────────────────────────────

def convert_single(
    pdf_path: Path,
    output_dir: Path,
    mode: str = "A",
    model_id: str | None = None,
    index: int | None = None,
    total: int | None = None,
) -> dict:
    """Convert a single PDF to Markdown. Returns stats dict."""
    pdf_size = pdf_path.stat().st_size
    prefix = f"[{index}/{total}]" if index and total else ""

    console.print(f"\n[cyan bold]{prefix} {pdf_path.name}[/cyan bold] [dim]({_format_size(pdf_size)})[/dim]")
    start = time.time()

    # Step 1: marker extraction
    result = subprocess.run(
        [str(MARKER_BIN), str(pdf_path), "--output_dir", str(output_dir)],
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

    # Step 3: optional API call
    api_time = 0
    api_usage = _empty_usage()
    if mode in ("B", "C") and model_id:
        est_tokens = _estimate_tokens(text)
        api_start = time.time()
        console.print(f"  ├ [blue]API:[/blue]     calling [cyan]{model_id.split('/')[-1]}[/cyan] "
                      f"(~{est_tokens:,} tokens est.)...")
        text, api_usage = call_api_chunked(text, mode, model_id)
        api_time = time.time() - api_start
        console.print(f"  ├ [blue]API:[/blue]     [yellow]{api_usage['prompt_tokens']:,}[/yellow] prompt + "
                      f"[yellow]{api_usage['completion_tokens']:,}[/yellow] completion = "
                      f"[yellow bold]{api_usage['total_tokens']:,}[/yellow bold] tokens  "
                      f"[dim]{api_time:.1f}s[/dim]")

    md_path.write_text(text, encoding='utf-8')

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
        "pages": meta["pages"],
        "images": meta["images"],
        "code_blocks": pp_stats["code_blocks"],
        "headings": pp_stats["headings"],
        "api_usage": api_usage,
        "out_size": out_size,
        "out_lines": out_lines,
        "time": elapsed,
        "marker_time": marker_time,
        "api_time": api_time,
    }


def _print_summary(results: list[dict], total_elapsed: float, mode: str, est_tokens: int = 0):
    """Print a rich summary table."""
    ok = [r for r in results if r["status"] == "ok"]
    fail = [r for r in results if r["status"] != "ok"]

    total_size = sum(r.get("out_size", 0) for r in ok)
    total_pages = sum(r.get("pages", 0) for r in ok)

    # Aggregate API usage
    total_usage = _empty_usage()
    for r in ok:
        for k in total_usage:
            total_usage[k] += r.get("api_usage", _empty_usage())[k]

    # Summary panel
    status_text = f"[green bold]✅ {len(ok)}/{len(results)} succeeded[/green bold]"
    if fail:
        status_text += f"  [red bold]❌ {len(fail)} failed[/red bold]"

    panel_lines = [
        status_text + f"   [dim]⏱ {total_elapsed:.0f}s total (avg {total_elapsed/max(len(results),1):.0f}s/file)[/dim]",
        f"[dim]📄 {total_pages} pages   💾 {_format_size(total_size)}[/dim]",
    ]

    if mode in ("B", "C") and total_usage["total_tokens"] > 0:
        token_line = (
            f"🔢 API tokens: [yellow]{total_usage['prompt_tokens']:,}[/yellow] prompt + "
            f"[yellow]{total_usage['completion_tokens']:,}[/yellow] completion = "
            f"[yellow bold]{total_usage['total_tokens']:,}[/yellow bold] total"
        )
        if est_tokens > 0:
            token_line += f"  [dim](est. ~{est_tokens:,})[/dim]"
        panel_lines.append(token_line)

    console.print()
    console.print(Panel("\n".join(panel_lines), title="[bold]Summary[/bold]", border_style="blue"))

    # Detail table
    table = Table(show_header=True, header_style="bold", border_style="dim")
    table.add_column("File", style="cyan", min_width=28)
    table.add_column("Pages", justify="right", style="yellow")
    table.add_column("Images", justify="right", style="yellow")
    table.add_column("Code", justify="right", style="yellow")
    if mode in ("B", "C"):
        table.add_column("Tokens", justify="right", style="yellow")
    table.add_column("Size", justify="right")
    table.add_column("Time", justify="right", style="dim")

    for r in results:
        if r["status"] == "ok":
            time_str = f"{r['time']:.0f}s"
            if r.get("api_time", 0) > 0:
                time_str = f"{r['marker_time']:.0f}s+{r['api_time']:.0f}s"

            row = [r["name"], str(r["pages"]), str(r["images"]), str(r["code_blocks"])]
            if mode in ("B", "C"):
                row.append(f"{r['api_usage']['total_tokens']:,}")
            row.extend([_format_size(r["out_size"]), time_str])
            table.add_row(*row)
        else:
            ncols = 7 if mode in ("B", "C") else 6
            row = [f"[red]{r['name']}[/red]"] + ["—"] * (ncols - 2) + ["[red]FAIL[/red]"]
            table.add_row(*row)

    # Totals row
    if len(ok) > 1:
        table.add_section()
        total_row = [
            "[bold]TOTAL[/bold]",
            f"[bold]{total_pages}[/bold]",
            f"[bold]{sum(r.get('images', 0) for r in ok)}[/bold]",
            f"[bold]{sum(r.get('code_blocks', 0) for r in ok)}[/bold]",
        ]
        if mode in ("B", "C"):
            total_row.append(f"[bold]{total_usage['total_tokens']:,}[/bold]")
        total_row.extend([
            f"[bold]{_format_size(total_size)}[/bold]",
            f"[bold]{total_elapsed:.0f}s[/bold]",
        ])
        table.add_row(*total_row)

    console.print(table)


def convert_all(pdf_dir: Path, output_dir: Path, mode: str = "A", model_id: str | None = None,
                est_tokens: int = 0):
    """Convert all PDFs in pdf_dir."""
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
                stats = convert_single(pdf, output_dir, mode, model_id, index=i, total=len(pdfs))
                results.append(stats)
                progress.start()
                progress.advance(task)
            except Exception as e:
                results.append({"name": pdf.name, "status": "error", "error": str(e)})
                progress.start()
                progress.advance(task)

    total_elapsed = time.time() - total_start
    _print_summary(results, total_elapsed, mode, est_tokens)


def main():
    # Parse optional filename from args (before flags)
    filename = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            filename = arg
            break

    # ── Step 1: Determine PDF list and scan pages ────────────────
    if filename:
        pdf_path = PDF_DIR / filename if not Path(filename).exists() else Path(filename)
        if not pdf_path.exists():
            console.print(f"[red bold]Error:[/red bold] {pdf_path} not found")
            sys.exit(1)
        pdf_list = [pdf_path]
    else:
        pdf_list = sorted(PDF_DIR.glob("*.pdf"))
        if not pdf_list:
            console.print(f"[red]No PDFs found in {PDF_DIR}/[/red]")
            sys.exit(1)

    # Quick scan: count pages (milliseconds, no OCR)
    scan = _scan_pdfs(pdf_list)

    # ── Step 2: Show info panel ──────────────────────────────────
    if filename:
        info = (f"📄 [cyan]{pdf_path.name}[/cyan] ({_format_size(scan['total_size'])})\n"
                f"📄 [yellow]{scan['total_pages']}[/yellow] pages")
    else:
        info = (f"📂 [bold]{len(pdf_list)}[/bold] PDFs in [cyan]{PDF_DIR}/[/cyan] "
                f"({_format_size(scan['total_size'])})\n"
                f"📄 [yellow]{scan['total_pages']}[/yellow] pages total")

    console.print(Panel(info, title="[bold]PDF → Markdown[/bold]", border_style="blue"))

    # ── Step 3: Interactive menu (with token estimates) ──────────
    mode, model_id, model_name = interactive_select(est_b=scan["est_b"], est_c=scan["est_c"])

    # Show selected mode
    mode_desc = MODES[mode]
    mode_line = f"🔧 Mode [bold]{mode}[/bold] — {mode_desc}"
    if model_name:
        mode_line += f"  [cyan]{model_name}[/cyan]"
    console.print(f"\n{mode_line}")

    # ── Step 4: Convert ──────────────────────────────────────────
    est_tokens = scan["est_b"] if mode == "B" else scan["est_c"] if mode == "C" else 0

    if filename:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stats = convert_single(pdf_path, OUTPUT_DIR, mode, model_id, index=1, total=1)
        _print_summary([stats], stats["time"], mode, est_tokens)
    else:
        convert_all(PDF_DIR, OUTPUT_DIR, mode, model_id, est_tokens)


if __name__ == "__main__":
    main()


# ── Standalone postprocess for Makefile target ───────────────────

def postprocess_file(md_path: Path) -> None:
    """Re-run postprocess on an existing markdown file (for `make postprocess`)."""
    text = md_path.read_text(encoding="utf-8")
    text, _stats = postprocess(text)
    md_path.write_text(text, encoding="utf-8")
