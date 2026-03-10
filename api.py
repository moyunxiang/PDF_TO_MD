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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, MofNCompleteColumn, SpinnerColumn

from convert import console, select_menu, select_menu_multi, _format_size

MARKDOWN_DIR = Path("markdown")
ENHANCED_DIR = Path("enhanced")

# ── Models ───────────────────────────────────────────────────────

MODELS_FILE = Path(__file__).parent / "models.json"


def _load_models() -> list[str]:
    """Load model IDs from models.json."""
    import json
    return json.loads(MODELS_FILE.read_text(encoding="utf-8"))


MODELS = _load_models()

MODES = {
    "cleanup": "Format cleanup (keep content, fix formatting)",
    "rewrite": "Understand + rewrite (reorganize into study guide)",
    "study": "中文学习笔记 (Chinese study notes, English terms)",
}

# ── Prompts ──────────────────────────────────────────────────────

PROMPTS_FILE = Path(__file__).parent / "prompts.json"


def _load_prompts() -> dict[str, str]:
    """Load prompts from prompts.json."""
    import json
    return json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))


PROMPTS = _load_prompts()

# ── Helpers ──────────────────────────────────────────────────────

def empty_usage() -> dict:
    """Return a zeroed token usage dict."""
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def estimate_tokens(text: str, mode: str = "cleanup") -> dict:
    """Estimate token usage from actual MD content.

    Returns dict with prompt_tokens, completion_tokens, total_tokens estimates.
    Much more accurate than page-count estimation.
    """
    # ~4 chars per token for English text
    prompt_tokens = max(1, len(text) // 4)
    # cleanup: output ≈ input (just reformatting)
    # rewrite: output ≈ 1.2× input (rewrite adds some)
    # study: output ≈ 1.5× input (detailed Chinese notes are longer than outline)
    if mode == "rewrite":
        completion_tokens = int(prompt_tokens * 1.2)
    elif mode == "study":
        completion_tokens = int(prompt_tokens * 1.5)
    else:
        completion_tokens = prompt_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _calc_dst(md: Path, source: dict, output_dir: Path, mode: str) -> Path:
    """Calculate destination path for an enhanced MD file.

    Naming: original stem + _mode + .md
      - markdown/a.md       → enhanced/a_study.md
      - markdown/b/c.md     → enhanced/b/c_study.md
      - markdown/b/sub/d.md → enhanced/b/sub/d_study.md

    Folder structure is preserved, only filenames get the mode suffix.
    """
    if source["type"] == "file":
        # Single file: output_dir is already the full target path
        return output_dir
    else:
        # Dir type: preserve relative path, rename file
        rel = md.relative_to(source["path"])
        new_name = f"{rel.stem}_{mode}.md"
        return output_dir / rel.parent / new_name


def _next_version(path: Path) -> Path:
    """Find next available version number for a path.

    c_study.md → c_study_1.md → c_study_2.md → ...
    """
    stem = path.stem    # e.g. "c_study"
    suffix = path.suffix  # ".md"
    parent = path.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


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
        "est_tokens_cleanup": estimate_tokens("x" * total_chars, "cleanup")["total_tokens"],
        "est_tokens_rewrite": estimate_tokens("x" * total_chars, "rewrite")["total_tokens"],
        "est_tokens_study": estimate_tokens("x" * total_chars, "study")["total_tokens"],
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
            md_files = sorted(item.rglob("*.md"))
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


def select_mode(est_cleanup: int = 0, est_rewrite: int = 0, est_study: int = 0) -> str | None:
    """Select enhancement mode. Returns mode string or None."""
    opt_cleanup = f"Cleanup — 格式整理      (~{est_cleanup:,} tokens)" if est_cleanup else "Cleanup — 格式整理"
    opt_rewrite = f"Rewrite — 理解重写      (~{est_rewrite:,} tokens)" if est_rewrite else "Rewrite — 理解重写"
    opt_study   = f"Study — 中文学习笔记    (~{est_study:,} tokens)" if est_study else "Study — 中文学习笔记"
    idx = select_menu("Enhancement mode:", [opt_cleanup, opt_rewrite, opt_study])
    if idx is None:
        return None
    return ["cleanup", "rewrite", "study"][idx]


def select_model() -> tuple[str, str] | None:
    """Select API model. Returns (model_id, model_name) or None."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        console.print("\n[red bold]✗ OPENROUTER_API_KEY not set.[/red bold]")
        console.print("  Export it first:  [cyan]export OPENROUTER_API_KEY=sk-or-...[/cyan]")
        return None

    options = [f"{i+1}) {m.split('/')[-1]}" for i, m in enumerate(MODELS)]
    idx = select_menu("Select model:", options)
    if idx is None:
        return None

    model_id = MODELS[idx]
    return model_id, model_id.split("/")[-1]


# ── API Call ─────────────────────────────────────────────────────

def call_api(text: str, mode: str, model_id: str, quiet: bool = False) -> tuple[str, dict]:
    """Send markdown to OpenRouter API with exponential backoff retry.

    Retries on failure: 1s → 2s → 4s → 8s → 16s → 32s, then gives up.
    Returns (result_text, usage_dict).
    """
    from openai import OpenAI

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
    )

    prompt = PROMPTS[mode]
    delays = [1, 2, 4, 8, 16, 32]
    last_err = None

    for attempt in range(len(delays) + 1):  # 0=first try, 1-6=retries
        try:
            response = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0.1 if mode == "cleanup" else 0.3,
            )

            usage = empty_usage()
            if response.usage:
                usage["prompt_tokens"] = getattr(response.usage, "prompt_tokens", 0) or 0
                usage["completion_tokens"] = getattr(response.usage, "completion_tokens", 0) or 0
                usage["total_tokens"] = getattr(response.usage, "total_tokens", 0) or 0

            return response.choices[0].message.content, usage

        except Exception as e:
            last_err = e
            if attempt < len(delays):
                wait = delays[attempt]
                if not quiet:
                    console.print(f"  [yellow]⚠ API error, retry in {wait}s: {e}[/yellow]")
                time.sleep(wait)
            else:
                raise last_err


def call_api_chunked(text: str, mode: str, model_id: str, chunk_limit: int = 60000,
                     quiet: bool = False) -> tuple[str, dict]:
    """For large files, split by # headings and process each chunk."""
    if len(text) <= chunk_limit:
        return call_api(text, mode, model_id, quiet=quiet)

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

    if not quiet:
        console.print(f"    [dim]split into {len(chunks)} chunks for API[/dim]")

    results = []
    total_usage = empty_usage()
    for i, chunk in enumerate(chunks):
        if not quiet:
            console.print(f"    [dim]chunk {i+1}/{len(chunks)} ({len(chunk)//1000}K chars)...[/dim]")
        result, usage = call_api(chunk, mode, model_id, quiet=quiet)
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

def _enhance_one(md: Path, dst: Path, mode: str, model_id: str) -> dict:
    """Process a single MD file via API. Thread-safe, no console output.

    Returns result dict with name, status, sizes, usage, time.
    """
    text = md.read_text(encoding="utf-8")
    start = time.time()
    enhanced_text, usage = call_api_chunked(text, mode, model_id, quiet=True)
    elapsed = time.time() - start
    dst.write_text(enhanced_text, encoding="utf-8")
    return {
        "name": dst.name,
        "status": "ok",
        "src_size": md.stat().st_size,
        "dst_size": dst.stat().st_size,
        "usage": usage,
        "time": elapsed,
    }


def _resolve_conflicts(tasks: list[tuple[Path, Path]]) -> tuple[list[tuple[Path, Path]], list[dict]]:
    """Check for existing files and ask user per-conflict.

    Returns (resolved_tasks, skipped_results).
    For each conflict, user chooses:
      - New version → rename to c_study_1.md, c_study_2.md, ...
      - Skip → add to skipped list
    """
    resolved = []
    skipped = []

    for md, dst in tasks:
        if dst.exists():
            next_ver = _next_version(dst)
            choice = select_menu(
                f"⚠️  {dst.name} already exists:",
                [f"New version → {next_ver.name}", "Skip"],
            )
            if choice is None or choice == 1:
                # Skip
                console.print(f"  [dim]⏭ {md.name} skipped[/dim]")
                skipped.append({"name": md.name, "status": "skipped"})
                continue
            else:
                # New version
                dst = next_ver
                console.print(f"  [dim]→ {dst.name}[/dim]")

        resolved.append((md, dst))

    return resolved, skipped


def enhance_all(source: dict, output_dir: Path, mode: str, model_id: str) -> list[dict]:
    """Enhance all MDs from a source, save to output_dir.

    Naming: files get _mode suffix, folders keep original name.
      - markdown/a.md       → enhanced/a_study.md
      - markdown/b/c.md     → enhanced/b/c_study.md

    If target exists, asks per-file: new version or skip.
    Non-skipped files are processed concurrently via ThreadPoolExecutor.
    """
    md_files = source["md_files"]
    if not md_files:
        console.print(f"[red]No MD files in source.[/red]")
        return []

    # Build (md, dst) task list
    raw_tasks = []
    for md in md_files:
        dst = _calc_dst(md, source, output_dir, mode)
        dst.parent.mkdir(parents=True, exist_ok=True)
        raw_tasks.append((md, dst))

    # Resolve conflicts (ask user per-file)
    tasks, skipped = _resolve_conflicts(raw_tasks)

    if not tasks:
        if skipped:
            console.print(f"[dim]All {len(skipped)} files skipped.[/dim]")
        return skipped

    n = len(tasks)
    results = list(skipped)  # start with skipped entries
    total_start = time.time()

    if n == 1:
        # Single file: run inline with verbose output (no threading overhead)
        md, dst = tasks[0]
        text = md.read_text(encoding="utf-8")
        est = estimate_tokens(text, mode)
        console.print(f"\n[cyan bold][1/1] {md.name}[/cyan bold] "
                      f"[dim]({_format_size(md.stat().st_size)}, ~{est['total_tokens']:,} tokens)[/dim]")
        try:
            api_start = time.time()
            enhanced_text, usage = call_api_chunked(text, mode, model_id)
            api_time = time.time() - api_start
            dst.write_text(enhanced_text, encoding="utf-8")

            console.print(f"  ├ [blue]tokens:[/blue]  [yellow]{usage['prompt_tokens']:,}[/yellow] prompt + "
                          f"[yellow]{usage['completion_tokens']:,}[/yellow] completion = "
                          f"[yellow bold]{usage['total_tokens']:,}[/yellow bold]")
            console.print(f"  └ [green]saved:[/green]  {dst}  [dim]{api_time:.1f}s[/dim]")
            results.append({
                "name": dst.name, "status": "ok",
                "src_size": md.stat().st_size, "dst_size": dst.stat().st_size,
                "usage": usage, "time": api_time,
            })
        except Exception as e:
            console.print(f"  [red]✗ Error: {e}[/red]")
            results.append({"name": dst.name, "status": "error", "error": str(e)})
    else:
        # Multiple files: full concurrency via ThreadPoolExecutor
        console.print(f"\n[bold]⚡ {n} files in parallel[/bold]")

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
            ptask = progress.add_task(
                f"[bold]Enhancing {n} files ({mode})[/bold]",
                total=n,
            )

            future_to_info = {}
            with ThreadPoolExecutor(max_workers=n) as executor:
                for md, dst in tasks:
                    future = executor.submit(_enhance_one, md, dst, mode, model_id)
                    future_to_info[future] = (md, dst)

                done_count = 0
                for future in as_completed(future_to_info):
                    md, dst = future_to_info[future]
                    try:
                        result = future.result()
                        progress.stop()
                        done_count += 1
                        console.print(
                            f"  [green]✓[/green] [{done_count}/{n}] [cyan]{result['name']}[/cyan]  "
                            f"[yellow]{result['usage']['total_tokens']:,}[/yellow] tokens  "
                            f"[dim]{result['time']:.1f}s[/dim]"
                        )
                        results.append(result)
                        progress.start()
                    except Exception as e:
                        progress.stop()
                        console.print(f"  [red]✗ {dst.name}: {e}[/red]")
                        results.append({"name": dst.name, "status": "error", "error": str(e)})
                        progress.start()
                    progress.advance(ptask)

    total_elapsed = time.time() - total_start
    _print_enhance_summary(results, total_elapsed, mode)

    # Copy images from source dir to enhanced dir (only for dir type)
    if source["type"] == "dir":
        src_dir = source["path"]
        for img in list(src_dir.rglob("*.jpeg")) + list(src_dir.rglob("*.png")):
            rel = img.relative_to(src_dir)
            img_dst = output_dir / rel
            img_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(img, img_dst)

    return results


def _print_enhance_summary(results: list[dict], total_elapsed: float, mode: str):
    """Print summary table for enhancement results."""
    ok = [r for r in results if r.get("status") == "ok"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    fail = [r for r in results if r.get("status") not in ("ok", "skipped")]

    total_usage = empty_usage()
    for r in ok:
        for k in total_usage:
            total_usage[k] += r.get("usage", {}).get(k, 0)

    status_text = f"[green bold]✅ {len(ok)}/{len(results)} enhanced[/green bold]"
    if skipped:
        status_text += f"  [dim]⏭ {len(skipped)} skipped[/dim]"
    if fail:
        status_text += f"  [red bold]❌ {len(fail)} failed[/red bold]"

    panel_lines = [
        status_text + f"   [dim]⏱ {total_elapsed:.0f}s total[/dim]",
    ]
    if ok:
        panel_lines.append(
            f"🔢 Tokens: [yellow]{total_usage['prompt_tokens']:,}[/yellow] prompt + "
            f"[yellow]{total_usage['completion_tokens']:,}[/yellow] completion = "
            f"[yellow bold]{total_usage['total_tokens']:,}[/yellow bold] total"
        )

    console.print()
    console.print(Panel("\n".join(panel_lines), title=f"[bold]Enhancement Summary ({mode})[/bold]", border_style="green"))

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
        elif r.get("status") == "skipped":
            table.add_row(f"[dim]{r['name']}[/dim]", "—", "—", "—", "[dim]SKIP[/dim]")
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

    # Build option strings (keep short to avoid line-wrap)
    options = []
    for s in sources:
        icon = "📁" if s["type"] == "dir" else "📄"
        n_files = len(s["md_files"])
        name = s["name"]
        if len(name) > 30:
            name = name[:27] + "..."
        options.append(
            f"{icon} {name}  ({n_files} files, {_format_size(s['total_size'])})"
        )

    # Single or multi select?
    if len(sources) > 1:
        sel_mode = select_menu("Selection mode:", ["Single select", "Multi select (Space to toggle)"])
        if sel_mode is None:
            console.print("[dim]Cancelled.[/dim]")
            return
        use_multi = sel_mode == 1
    else:
        use_multi = False

    # Select source(s)
    if use_multi:
        indices = select_menu_multi("Select sources to enhance:", options)
        if not indices:
            console.print("[dim]Cancelled.[/dim]")
            return
        selected_list = [sources[i] for i in indices]
    else:
        idx = select_menu("Select source to enhance:", options)
        if idx is None:
            console.print("[dim]Cancelled.[/dim]")
            return
        selected_list = [sources[idx]]

    # Aggregate token estimates across all selected sources
    total_est_cleanup = sum(s["est_tokens_cleanup"] for s in selected_list)
    total_est_rewrite = sum(s["est_tokens_rewrite"] for s in selected_list)
    total_est_study = sum(s["est_tokens_study"] for s in selected_list)
    total_files = sum(len(s["md_files"]) for s in selected_list)

    # Select mode
    mode = select_mode(est_cleanup=total_est_cleanup, est_rewrite=total_est_rewrite, est_study=total_est_study)
    if mode is None:
        console.print("[dim]Cancelled.[/dim]")
        return

    # Show precise token estimate
    est_key = f"est_tokens_{mode}"
    total_est = sum(s[est_key] for s in selected_list)
    console.print(f"\n🔧 Mode [bold]{mode}[/bold] — {MODES[mode]}")
    console.print(f"📊 Estimated tokens: [yellow bold]~{total_est:,}[/yellow bold]")

    # Select model
    result = select_model()
    if result is None:
        return
    model_id, model_name = result
    console.print(f"🤖 Model: [cyan]{model_name}[/cyan]")

    # Show plan — folder keeps original name, files get _mode suffix
    console.print(f"\n📋 [bold]{len(selected_list)} source{'s' if len(selected_list) > 1 else ''}, {total_files} files:[/bold]")
    for s in selected_list:
        if s["type"] == "dir":
            out = ENHANCED_DIR / s["name"]
            console.print(f"  {s['name']}/ → [cyan]{out}/[/cyan]  (files: *_{mode}.md)")
        else:
            stem = Path(s["name"]).stem
            out = ENHANCED_DIR / f"{stem}_{mode}.md"
            console.print(f"  {s['name']} → [cyan]{out}[/cyan]")
    if total_files > 1:
        console.print(f"⚡ Concurrency: [bold]{total_files} files in parallel[/bold]")

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

    # Run enhancement for each selected source
    for s in selected_list:
        if s["type"] == "dir":
            output_dir = ENHANCED_DIR / s["name"]
        else:
            output_dir = ENHANCED_DIR / f"{Path(s['name']).stem}_{mode}.md"

        enhance_all(s, output_dir, mode, model_id)

        console.print(f"\n[bold]Enhanced output:[/bold] [cyan]{output_dir}[/cyan]")
        if s["type"] == "dir":
            for md in sorted(output_dir.rglob("*.md")):
                console.print(f"  📄 {md.name}  ({_format_size(md.stat().st_size)})")
        else:
            if output_dir.exists():
                console.print(f"  📄 {output_dir.name}  ({_format_size(output_dir.stat().st_size)})")
