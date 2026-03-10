#!/usr/bin/env python3
"""
API enhancement module — standalone step to enhance Markdown via LLM (OpenRouter).

Can be used independently after conversion:
  1. Convert PDFs → markdown/ (pure marker)
  2. Enhance markdown/ MDs → enhanced/ (this module)

Also supports manually placed .md files in markdown/.
"""

import os
import re
import shutil
import sys
import time
from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, MofNCompleteColumn, SpinnerColumn

from convert import console, select_menu, _format_size

MARKDOWN_DIR = Path("markdown")
ENHANCED_DIR = Path("enhanced")

# ── Models ───────────────────────────────────────────────────────

MODELS = {
    "1": {"name": "gpt-4o-mini",           "id": "openai/gpt-4o-mini"},
    "2": {"name": "deepseek-v3.2",         "id": "deepseek/deepseek-chat-v3-0324"},
    "3": {"name": "qwen2.5-72b-instruct",  "id": "qwen/qwen-2.5-72b-instruct"},
}

MODES = {
    "B": "Format cleanup (keep content, fix formatting)",
    "C": "Understand + rewrite (reorganize into study guide)",
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

def empty_usage() -> dict:
    """Return a zeroed token usage dict."""
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def estimate_tokens(text: str, mode: str = "B") -> dict:
    """Estimate token usage from actual MD content.

    Returns dict with prompt_tokens, completion_tokens, total_tokens estimates.
    Much more accurate than page-count estimation.
    """
    # ~4 chars per token for English text
    prompt_tokens = max(1, len(text) // 4)
    # Mode B: output ≈ input (just reformatting)
    # Mode C: output ≈ 1.2× input (rewrite adds some)
    if mode == "C":
        completion_tokens = int(prompt_tokens * 1.2)
    else:
        completion_tokens = prompt_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


# ── Scan MD Sources ──────────────────────────────────────────────

def _scan_one(md_files: list[Path], name: str, path: Path, source_type: str) -> dict:
    """Compute stats for a list of MD files."""
    total_lines = 0
    total_size = 0
    total_chars = 0
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        total_lines += len(text.splitlines())
        total_size += md.stat().st_size
        total_chars += len(text)

    return {
        "type": source_type,      # "dir" or "file"
        "name": name,
        "path": path,
        "md_files": md_files,
        "total_lines": total_lines,
        "total_size": total_size,
        "est_tokens_b": estimate_tokens("x" * total_chars, "B")["total_tokens"],
        "est_tokens_c": estimate_tokens("x" * total_chars, "C")["total_tokens"],
    }


def scan_md_sources() -> list[dict]:
    """Scan markdown/ for directories and loose .md files.

    Detects two formats:
      - markdown/abc/  (directory with .md files inside) → type="dir"
      - markdown/abc.md (single .md file)                → type="file"

    Returns list of source dicts sorted by name.
    """
    results = []
    if not MARKDOWN_DIR.exists():
        return results

    for item in sorted(MARKDOWN_DIR.iterdir()):
        if item.name.startswith("."):
            continue

        if item.is_dir():
            # Directory: look for .md files inside
            md_files = sorted(item.glob("*.md"))
            if not md_files:
                continue
            results.append(_scan_one(md_files, item.name, item, "dir"))

        elif item.is_file() and item.suffix == ".md":
            # Loose .md file
            results.append(_scan_one([item], item.name, item, "file"))

    return results


def scan_single_dir(md_dir: Path) -> dict:
    """Scan a single directory for MD files. Returns stats dict."""
    md_files = sorted(md_dir.glob("*.md"))
    return _scan_one(md_files, md_dir.name, md_dir, "dir")


# ── Interactive Selection ────────────────────────────────────────


def select_mode(est_b: int = 0, est_c: int = 0) -> str | None:
    """Select enhancement mode B or C. Returns mode string or None."""
    opt_b = f"B) Format cleanup         (~{est_b:,} tokens)" if est_b else "B) Format cleanup"
    opt_c = f"C) Understand + rewrite   (~{est_c:,} tokens)" if est_c else "C) Understand + rewrite"
    idx = select_menu("Enhancement mode:", [opt_b, opt_c])
    if idx is None:
        return None
    return "B" if idx == 0 else "C"


def select_model() -> tuple[str, str] | None:
    """Select API model. Returns (model_id, model_name) or None."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        console.print("\n[red bold]✗ OPENROUTER_API_KEY not set.[/red bold]")
        console.print("  Export it first:  [cyan]export OPENROUTER_API_KEY=sk-or-...[/cyan]")
        return None

    options = [f"{k}) {v['name']}" for k, v in MODELS.items()]
    idx = select_menu("Select model:", options)
    if idx is None:
        return None

    key = list(MODELS.keys())[idx]
    return MODELS[key]["id"], MODELS[key]["name"]


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

    usage = empty_usage()
    if response.usage:
        usage["prompt_tokens"] = getattr(response.usage, "prompt_tokens", 0) or 0
        usage["completion_tokens"] = getattr(response.usage, "completion_tokens", 0) or 0
        usage["total_tokens"] = getattr(response.usage, "total_tokens", 0) or 0

    return response.choices[0].message.content, usage


def call_api_chunked(text: str, mode: str, model_id: str, chunk_limit: int = 60000) -> tuple[str, dict]:
    """For large files, split by # headings and process each chunk."""
    if len(text) <= chunk_limit:
        return call_api(text, mode, model_id)

    sections = re.split(r'(?=^# )', text, flags=re.MULTILINE)
    sections = [s for s in sections if s.strip()]

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
    total_usage = empty_usage()
    for i, chunk in enumerate(chunks):
        console.print(f"    [dim]chunk {i+1}/{len(chunks)} ({len(chunk)//1000}K chars)...[/dim]")
        result, usage = call_api(chunk, mode, model_id)
        results.append(result)
        for k in total_usage:
            total_usage[k] += usage[k]

    return "\n\n".join(results), total_usage


def enhance_file(md_path: Path, mode: str, model_id: str) -> dict:
    """Read a markdown file, enhance via API, write back. Returns usage dict."""
    md_path = Path(md_path)
    text = md_path.read_text(encoding="utf-8")
    est = estimate_tokens(text, mode)

    console.print(f"  ├ [blue]API:[/blue]     calling [cyan]{model_id.split('/')[-1]}[/cyan] "
                  f"(~{est['total_tokens']:,} tokens est.)...")

    text, usage = call_api_chunked(text, mode, model_id)

    console.print(f"  ├ [blue]API:[/blue]     [yellow]{usage['prompt_tokens']:,}[/yellow] prompt + "
                  f"[yellow]{usage['completion_tokens']:,}[/yellow] completion = "
                  f"[yellow bold]{usage['total_tokens']:,}[/yellow bold] tokens")

    md_path.write_text(text, encoding="utf-8")
    return usage


# ── Batch Enhance ────────────────────────────────────────────────

def enhance_all(source: dict, output_target: Path, mode: str, model_id: str) -> list[dict]:
    """Enhance all MDs from a source, save to output_target.

    source: dict from scan_md_sources() with type, md_files, path, etc.
    output_target: for type="dir" → a directory; for type="file" → a file path.
    """
    md_files = source["md_files"]
    if not md_files:
        console.print(f"[red]No MD files in source.[/red]")
        return []

    # Prepare output location
    if source["type"] == "dir":
        output_target.mkdir(parents=True, exist_ok=True)
    else:
        output_target.parent.mkdir(parents=True, exist_ok=True)

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
            f"[bold]Enhancing {len(md_files)} file{'s' if len(md_files) > 1 else ''} (Mode {mode})[/bold]",
            total=len(md_files),
        )

        for i, md in enumerate(md_files, 1):
            try:
                progress.stop()

                text = md.read_text(encoding="utf-8")
                est = estimate_tokens(text, mode)

                # Determine destination path
                if source["type"] == "dir":
                    dst = output_target / md.name
                else:
                    dst = output_target

                console.print(f"\n[cyan bold][{i}/{len(md_files)}] {md.name}[/cyan bold] "
                              f"[dim]({_format_size(md.stat().st_size)}, ~{est['total_tokens']:,} tokens)[/dim]")

                api_start = time.time()
                enhanced_text, usage = call_api_chunked(text, mode, model_id)
                api_time = time.time() - api_start

                dst.write_text(enhanced_text, encoding="utf-8")

                console.print(f"  ├ [blue]tokens:[/blue]  [yellow]{usage['prompt_tokens']:,}[/yellow] prompt + "
                              f"[yellow]{usage['completion_tokens']:,}[/yellow] completion = "
                              f"[yellow bold]{usage['total_tokens']:,}[/yellow bold]")
                console.print(f"  └ [green]saved:[/green]  {dst}  [dim]{api_time:.1f}s[/dim]")

                results.append({
                    "name": md.name,
                    "status": "ok",
                    "src_size": md.stat().st_size,
                    "dst_size": dst.stat().st_size,
                    "usage": usage,
                    "time": api_time,
                })

                progress.start()
                progress.advance(task)

            except Exception as e:
                console.print(f"  [red]✗ Error: {e}[/red]")
                results.append({"name": md.name, "status": "error", "error": str(e)})
                progress.start()
                progress.advance(task)

    total_elapsed = time.time() - total_start
    _print_enhance_summary(results, total_elapsed, mode)

    # Copy images from source dir to enhanced dir (only for dir type)
    if source["type"] == "dir":
        src_dir = source["path"]
        for img in list(src_dir.glob("*.jpeg")) + list(src_dir.glob("*.png")):
            shutil.copy2(img, output_target / img.name)

    return results


def _print_enhance_summary(results: list[dict], total_elapsed: float, mode: str):
    """Print summary table for enhancement results."""
    ok = [r for r in results if r.get("status") == "ok"]
    fail = [r for r in results if r.get("status") != "ok"]

    total_usage = empty_usage()
    for r in ok:
        for k in total_usage:
            total_usage[k] += r.get("usage", {}).get(k, 0)

    status_text = f"[green bold]✅ {len(ok)}/{len(results)} enhanced[/green bold]"
    if fail:
        status_text += f"  [red bold]❌ {len(fail)} failed[/red bold]"

    panel_lines = [
        status_text + f"   [dim]⏱ {total_elapsed:.0f}s total[/dim]",
        f"🔢 Tokens: [yellow]{total_usage['prompt_tokens']:,}[/yellow] prompt + "
        f"[yellow]{total_usage['completion_tokens']:,}[/yellow] completion = "
        f"[yellow bold]{total_usage['total_tokens']:,}[/yellow bold] total",
    ]

    console.print()
    console.print(Panel("\n".join(panel_lines), title=f"[bold]Enhancement Summary (Mode {mode})[/bold]", border_style="green"))

    table = Table(show_header=True, header_style="bold", border_style="dim")
    table.add_column("File", style="cyan", min_width=28)
    table.add_column("Original", justify="right")
    table.add_column("Enhanced", justify="right")
    table.add_column("Tokens", justify="right", style="yellow")
    table.add_column("Time", justify="right", style="dim")

    for r in results:
        if r.get("status") == "ok":
            table.add_row(
                r["name"],
                _format_size(r["src_size"]),
                _format_size(r["dst_size"]),
                f"{r['usage']['total_tokens']:,}",
                f"{r['time']:.0f}s",
            )
        else:
            table.add_row(f"[red]{r['name']}[/red]", "—", "—", "—", "[red]FAIL[/red]")

    if len(ok) > 1:
        table.add_section()
        table.add_row(
            "[bold]TOTAL[/bold]",
            f"[bold]{_format_size(sum(r['src_size'] for r in ok))}[/bold]",
            f"[bold]{_format_size(sum(r['dst_size'] for r in ok))}[/bold]",
            f"[bold]{total_usage['total_tokens']:,}[/bold]",
            f"[bold]{total_elapsed:.0f}s[/bold]",
        )

    console.print(table)


# ── Interactive Enhance Flow ─────────────────────────────────────

def enhance_interactive():
    """Full interactive flow: scan markdown/ → select → mode → model → enhance."""
    sources = scan_md_sources()

    if not sources:
        console.print(f"[red]No Markdown found in {MARKDOWN_DIR}/.[/red]")
        console.print("[dim]Run conversion first: make convert[/dim]")
        console.print(f"[dim]Or place .md files directly in {MARKDOWN_DIR}/[/dim]")
        return

    # Show available sources
    info_lines = []
    for s in sources:
        icon = "📁" if s["type"] == "dir" else "📄"
        n_files = len(s["md_files"])
        files_label = f"{n_files} file{'s' if n_files > 1 else ''}"
        info_lines.append(
            f"{icon} [cyan]{s['name']}{'/' if s['type'] == 'dir' else ''}[/cyan]  "
            f"{files_label} · {s['total_lines']:,} lines · {_format_size(s['total_size'])}"
        )
    console.print(Panel("\n".join(info_lines), title="[bold]Available Markdown[/bold]", border_style="blue"))

    # Select source
    options = []
    for s in sources:
        icon = "📁" if s["type"] == "dir" else "📄"
        n_files = len(s["md_files"])
        files_label = f"{n_files} file{'s' if n_files > 1 else ''}"
        options.append(
            f"{icon} {s['name']}{'/' if s['type'] == 'dir' else ''}  "
            f"({files_label}, {_format_size(s['total_size'])}, "
            f"~{s['est_tokens_b']:,}B / ~{s['est_tokens_c']:,}C tokens)"
        )
    idx = select_menu("Select source to enhance:", options)
    if idx is None:
        console.print("[dim]Cancelled.[/dim]")
        return

    selected = sources[idx]

    # Select mode
    mode = select_mode(est_b=selected["est_tokens_b"], est_c=selected["est_tokens_c"])
    if mode is None:
        console.print("[dim]Cancelled.[/dim]")
        return

    # Show precise token estimate
    est_key = "est_tokens_b" if mode == "B" else "est_tokens_c"
    est = selected[est_key]
    console.print(f"\n🔧 Mode [bold]{mode}[/bold] — {MODES[mode]}")
    console.print(f"📊 Estimated tokens: [yellow bold]~{est:,}[/yellow bold]")

    # Select model
    result = select_model()
    if result is None:
        return
    model_id, model_name = result
    console.print(f"🤖 Model: [cyan]{model_name}[/cyan]")

    # Determine output target
    if selected["type"] == "dir":
        output_target = ENHANCED_DIR / selected["name"]
    else:
        output_target = ENHANCED_DIR / selected["name"]
    console.print(f"📂 Output: [cyan]{output_target}[/cyan]")

    # Confirm
    console.print()
    try:
        confirm = input("Proceed? [Y/n]: ").strip().lower()
        if confirm and confirm != "y":
            console.print("[dim]Cancelled.[/dim]")
            return
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]Cancelled.[/dim]")
        return

    # Run enhancement
    enhance_all(selected, output_target, mode, model_id)

    console.print(f"\n[bold]Enhanced output:[/bold] [cyan]{output_target}[/cyan]")
    if selected["type"] == "dir":
        for md in sorted(output_target.glob("*.md")):
            console.print(f"  📄 {md.name}  ({_format_size(md.stat().st_size)})")
    else:
        if output_target.exists():
            console.print(f"  📄 {output_target.name}  ({_format_size(output_target.stat().st_size)})")
