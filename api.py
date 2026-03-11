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

PDF_MODELS_FILE = Path(__file__).parent / "pdf_models.json"


def _load_pdf_models() -> list[str]:
    """Load model for PDF conversion IDs from pdf_models.json."""
    import json
    return json.loads(PDF_MODELS_FILE.read_text(encoding="utf-8"))


PDF_MODELS = _load_pdf_models()


# ── Pricing (cached locally, updated via `make pricing`) ─────────

PRICING_FILE = Path(__file__).parent / "pricing.json"
MODEL_PRICING: dict[str, tuple[float, float]] = {}


def _fetch_pricing_from_api() -> dict[str, dict]:
    """Fetch live model pricing from OpenRouter (free, no API key needed).

    Returns raw dict: model_id → {"input": x, "output": y, "name": "..."}.
    """
    import json
    import urllib.request

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "pdf-to-md/1.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())

    result = {}
    for m in data.get("data", []):
        mid = m["id"]
        p = m.get("pricing", {})
        inp = float(p.get("prompt", 0)) * 1_000_000   # per-token → per-1M
        out = float(p.get("completion", 0)) * 1_000_000
        if inp > 0 or out > 0:
            result[mid] = {"input": round(inp, 4), "output": round(out, 4), "name": m.get("name", mid)}
    return result


def update_pricing(quiet: bool = False) -> int:
    """Fetch latest pricing from OpenRouter and save to pricing.json.

    Returns number of models saved. Raises on network failure.
    """
    import json

    raw = _fetch_pricing_from_api()
    # Sort by model ID for readability
    sorted_data = dict(sorted(raw.items()))

    # Save with metadata
    out = {
        "_updated": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
        "_count": len(sorted_data),
        "models": sorted_data,
    }
    PRICING_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    if not quiet:
        console.print(f"[green]✓ pricing.json updated: {len(sorted_data)} models[/green]")
    return len(sorted_data)


def _load_pricing() -> dict[str, tuple[float, float]]:
    """Load pricing from local pricing.json cache.

    Returns dict: model_id → (input_per_1M, output_per_1M).
    Returns empty dict if file missing (run `make pricing` first).
    """
    import json

    if not PRICING_FILE.exists():
        return {}
    try:
        data = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
        pricing = {}
        for mid, info in data.get("models", {}).items():
            pricing[mid] = (info["input"], info["output"])
        return pricing
    except Exception:
        return {}


def _init_pricing():
    """Load cached pricing at startup. If no cache, fetch once."""
    global MODEL_PRICING
    MODEL_PRICING = _load_pricing()
    if MODEL_PRICING:
        # Check age — show hint if older than 7 days
        import json, datetime
        try:
            data = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
            updated = datetime.datetime.strptime(data["_updated"], "%Y-%m-%d %H:%M")
            age_days = (datetime.datetime.now() - updated).days
            if age_days > 7:
                console.print(f"[dim]💲 Pricing loaded ({len(MODEL_PRICING)} models, {age_days}d old — run `make pricing` to refresh)[/dim]")
        except Exception:
            pass
    else:
        # No cache — fetch for first time
        try:
            console.print("[dim]💲 First run: fetching pricing from OpenRouter...[/dim]")
            update_pricing(quiet=True)
            MODEL_PRICING = _load_pricing()
            console.print(f"[dim]💲 pricing.json created ({len(MODEL_PRICING)} models)[/dim]")
        except Exception:
            console.print("[dim]💲 Could not fetch pricing (offline?) — costs will show as $0[/dim]")


_init_pricing()


def estimate_cost(prompt_tokens: int, completion_tokens: int, model_id: str) -> float:
    """Estimate cost in USD given token counts and model ID.

    Uses live pricing from OpenRouter API.
    Returns 0.0 if model not found or pricing unavailable.
    """
    inp_price, out_price = MODEL_PRICING.get(model_id, (0, 0))
    return (prompt_tokens * inp_price + completion_tokens * out_price) / 1_000_000


def format_cost(cost: float) -> str:
    """Format a USD cost for display."""
    if cost < 0.001:
        return f"${cost:.4f}"
    elif cost < 1.0:
        return f"${cost:.3f}"
    else:
        return f"${cost:.2f}"


MODES = {
    "cleanup": "Format cleanup (keep content, fix formatting)",
    "rewrite": "Understand + rewrite (reorganize into study guide)",
    "study": "中文复习笔记 (review notes, concise)",
    "tutorial": "中文详细教程 (deep tutorial, every point explained)",
}

# ── Prompts ──────────────────────────────────────────────────────

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompts() -> dict[str, str]:
    """Load prompts from prompts/*.txt (one file per mode)."""
    prompts = {}
    for f in sorted(PROMPTS_DIR.glob("*.txt")):
        prompts[f.stem] = f.read_text(encoding="utf-8")
    return prompts


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
    # study: output ≈ 1.5× input (Chinese review notes)
    # tutorial: output ≈ 2.5× input (deep tutorial, every point + every problem explained)
    if mode == "rewrite":
        completion_tokens = int(prompt_tokens * 1.2)
    elif mode == "study":
        completion_tokens = int(prompt_tokens * 1.5)
    elif mode == "tutorial":
        completion_tokens = int(prompt_tokens * 2.5)
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
        "est_tokens_tutorial": estimate_tokens("x" * total_chars, "tutorial")["total_tokens"],
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


def select_mode(est_cleanup: int = 0, est_rewrite: int = 0, est_study: int = 0, est_tutorial: int = 0) -> str | None:
    """Select enhancement mode. Returns mode string or None (back)."""
    opt_cleanup  = f"Cleanup — 格式整理        (~{est_cleanup:,} tokens)" if est_cleanup else "Cleanup — 格式整理"
    opt_rewrite  = f"Rewrite — 理解重写        (~{est_rewrite:,} tokens)" if est_rewrite else "Rewrite — 理解重写"
    opt_study    = f"Study — 中文复习笔记      (~{est_study:,} tokens)" if est_study else "Study — 中文复习笔记"
    opt_tutorial = f"Tutorial — 中文详细教程   (~{est_tutorial:,} tokens)" if est_tutorial else "Tutorial — 中文详细教程"
    idx = select_menu("Enhancement mode:", [opt_cleanup, opt_rewrite, opt_study, opt_tutorial], show_back=True)
    if idx is None:
        return None
    return ["cleanup", "rewrite", "study", "tutorial"][idx]


def select_model(est_prompt: int = 0, est_completion: int = 0) -> tuple[str, str] | None:
    """Select API model. Returns (model_id, model_name) or None (back).

    If est_prompt/est_completion provided, shows estimated cost per model.
    Note: caller should check OPENROUTER_API_KEY before calling this.
    """
    options = []
    for m in MODELS:
        name = m.split("/")[-1]
        inp, out = MODEL_PRICING.get(m, (0, 0))
        price_str = f"${inp:.2f}/${out:.2f} per 1M" if inp else ""
        if (est_prompt or est_completion) and inp:
            cost = estimate_cost(est_prompt, est_completion, m)
            options.append(f"{name:28s}  ~{format_cost(cost):8s}  ({price_str})")
        elif price_str:
            options.append(f"{name:28s}  {price_str}")
        else:
            options.append(name)
    idx = select_menu("Select model:", options, show_back=True)
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

    file_cost = estimate_cost(usage["prompt_tokens"], usage["completion_tokens"], model_id)
    console.print(f"  ├ [blue]API:[/blue]     [yellow]{usage['prompt_tokens']:,}[/yellow] prompt + "
                  f"[yellow]{usage['completion_tokens']:,}[/yellow] completion = "
                  f"[yellow bold]{usage['total_tokens']:,}[/yellow bold] tokens  "
                  f"💰 [green]{format_cost(file_cost)}[/green]")

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
        "model_id": model_id,
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

            single_cost = estimate_cost(usage["prompt_tokens"], usage["completion_tokens"], model_id)
            console.print(f"  ├ [blue]tokens:[/blue]  [yellow]{usage['prompt_tokens']:,}[/yellow] prompt + "
                          f"[yellow]{usage['completion_tokens']:,}[/yellow] completion = "
                          f"[yellow bold]{usage['total_tokens']:,}[/yellow bold]  "
                          f"💰 [green]{format_cost(single_cost)}[/green]")
            console.print(f"  └ [green]saved:[/green]  {dst}  [dim]{api_time:.1f}s[/dim]")
            results.append({
                "name": dst.name, "status": "ok",
                "src_size": md.stat().st_size, "dst_size": dst.stat().st_size,
                "usage": usage, "time": api_time, "model_id": model_id,
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
                        par_cost = estimate_cost(result["usage"]["prompt_tokens"], result["usage"]["completion_tokens"], result.get("model_id", ""))
                        console.print(
                            f"  [green]✓[/green] [{done_count}/{n}] [cyan]{result['name']}[/cyan]  "
                            f"[yellow]{result['usage']['total_tokens']:,}[/yellow] tokens  "
                            f"💰 [green]{format_cost(par_cost)}[/green]  "
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
        # Add cost if model_id is available in results
        first_ok = ok[0]
        if "model_id" in first_ok:
            total_cost = estimate_cost(total_usage["prompt_tokens"], total_usage["completion_tokens"], first_ok["model_id"])
            panel_lines.append(f"💰 Cost: [green bold]{format_cost(total_cost)}[/green bold]")

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


# ── PDF → MD via Vision API ───────────────────────────────────────

def _render_pdf_pages(pdf_path: Path, dpi: int = 150) -> list[str]:
    """Render all PDF pages to base64 JPEG strings.

    Returns list of base64-encoded JPEG images (one per page).
    Used by the 'image' input method.
    """
    import base64
    import io
    import pypdfium2

    doc = pypdfium2.PdfDocument(str(pdf_path))
    images = []
    scale = dpi / 72  # pypdfium2 default is 72 DPI

    for i in range(len(doc)):
        page = doc[i]
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil()

        buf = io.BytesIO()
        pil_image.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
        images.append(b64)

    doc.close()
    return images


def select_pdf_model(est_prompt: int = 0, est_completion: int = 0) -> tuple[str, str] | None:
    """Select a model for PDF conversion. Returns (model_id, model_name) or None.

    If est_prompt/est_completion provided, shows estimated cost per model.
    """
    options = []
    for m in PDF_MODELS:
        name = m.split("/")[-1]
        inp, out = MODEL_PRICING.get(m, (0, 0))
        price_str = f"${inp:.2f}/${out:.2f} per 1M" if inp else ""
        if (est_prompt or est_completion) and inp:
            cost = estimate_cost(est_prompt, est_completion, m)
            options.append(f"{name:28s}  ~{format_cost(cost):8s}  ({price_str})")
        elif price_str:
            options.append(f"{name:28s}  {price_str}")
        else:
            options.append(name)
    idx = select_menu("Select model:", options, show_back=True)
    if idx is None:
        return None
    model_id = PDF_MODELS[idx]
    return model_id, model_id.split("/")[-1]


def _extract_pdf_page_range(pdf_path: Path, start: int, end: int) -> bytes:
    """Extract a page range from a PDF and return as bytes.

    Args:
        pdf_path: source PDF
        start: start page index (0-based, inclusive)
        end: end page index (0-based, exclusive)

    Returns: PDF bytes of the sub-document
    """
    import pypdfium2

    doc = pypdfium2.PdfDocument(str(pdf_path))
    new_doc = pypdfium2.PdfDocument.new()
    new_doc.import_pages(doc, list(range(start, end)))

    import io
    buf = io.BytesIO()
    new_doc.save(buf)
    pdf_bytes = buf.getvalue()

    new_doc.close()
    doc.close()
    return pdf_bytes


def _call_pdf_api(pdf_bytes: bytes, filename: str, model_id: str,
                  prompt: str, quiet: bool = False) -> tuple[str, dict]:
    """Send a PDF file directly to the API.

    Uses the OpenAI-compatible 'file' content type.
    Much more efficient than rendering to images — smaller payload, fewer tokens.
    Returns (markdown_text, usage_dict).
    """
    import base64
    from openai import OpenAI

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
    )

    pdf_b64 = base64.b64encode(pdf_bytes).decode()

    content = [
        {"type": "text", "text": prompt},
        {
            "type": "file",
            "file": {
                "filename": filename,
                "file_data": f"data:application/pdf;base64,{pdf_b64}",
            },
        },
    ]

    delays = [1, 2, 4, 8, 16, 32]
    last_err = None

    for attempt in range(len(delays) + 1):
        try:
            response = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "user", "content": content},
                ],
                temperature=0.1,
                max_tokens=16000,
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


def _call_image_api(images_b64: list[str], model_id: str,
                    prompt: str, quiet: bool = False) -> tuple[str, dict]:
    """Send page images to a vision model and get markdown back.

    Sends all images in a single request as multiple image_url content parts.
    Returns (markdown_text, usage_dict).
    """
    from openai import OpenAI

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
    )

    content = [{"type": "text", "text": prompt}]
    for b64 in images_b64:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{b64}",
            },
        })

    delays = [1, 2, 4, 8, 16, 32]
    last_err = None

    for attempt in range(len(delays) + 1):
        try:
            response = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "user", "content": content},
                ],
                temperature=0.1,
                max_tokens=16000,
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


def convert_pdf_via_api(
    pdf_path: Path,
    output_dir: Path,
    model_id: str,
    input_method: str = "pdf",
    pages_per_batch: int = 30,
    index: int | None = None,
    total: int | None = None,
) -> dict:
    """Convert a single PDF to Markdown via API.

    Supports two input methods:
      - "pdf":   send PDF file directly (smaller payload, fewer tokens, default)
      - "image": render pages to JPEG images then send (fallback if PDF fails)

    Large PDFs are split into batches of pages_per_batch.

    Args:
        pdf_path: Path to PDF
        output_dir: Directory to save output (creates stem/ subdirectory)
        model_id: OpenRouter model ID
        input_method: "pdf" (default) or "image"
        pages_per_batch: Max pages per API call (default 30 for PDF, 10 for image)
        index/total: For display (e.g. [2/5])

    Returns: stats dict (same shape as convert_single for compatibility)
    """
    import pypdfium2

    pdf_size = pdf_path.stat().st_size
    prefix = f"[{index}/{total}]" if index and total else ""
    stem = pdf_path.stem

    # Skip if already converted
    part_out_dir = output_dir / stem
    existing_md = part_out_dir / f"{stem}.md"
    if existing_md.exists() and existing_md.stat().st_size > 0:
        console.print(f"\n[dim]{prefix} {pdf_path.name} — already converted, skipping[/dim]")
        return {"name": pdf_path.name, "status": "skipped"}

    # Get page count
    doc = pypdfium2.PdfDocument(str(pdf_path))
    n_pages = len(doc)
    doc.close()

    method_label = "PDF direct" if input_method == "pdf" else "images"
    console.print(f"\n[cyan bold]{prefix} {pdf_path.name}[/cyan bold] "
                  f"[dim]({_format_size(pdf_size)}, {n_pages} pages, {method_label})[/dim]")

    start = time.time()
    prompt = PROMPTS.get("convert", "Convert these lecture slides to structured Markdown notes.")
    total_usage = empty_usage()
    md_parts = []

    # Adjust batch size for image mode (images are much larger per page)
    effective_batch = pages_per_batch if input_method == "pdf" else min(pages_per_batch, 10)
    n_batches = (n_pages + effective_batch - 1) // effective_batch

    if input_method == "pdf":
        # ── PDF direct mode ──────────────────────────────────────
        for batch_idx in range(n_batches):
            batch_start = batch_idx * effective_batch
            batch_end = min(batch_start + effective_batch, n_pages)
            batch_label = f"p.{batch_start+1}-{batch_end}"

            console.print(f"  ├ [blue]API:[/blue]     batch {batch_idx+1}/{n_batches} "
                          f"({batch_label}, {batch_end - batch_start} pages)...")

            # Extract sub-PDF for this batch (or use whole file if single batch)
            if n_batches == 1:
                pdf_bytes = pdf_path.read_bytes()
                fname = pdf_path.name
            else:
                pdf_bytes = _extract_pdf_page_range(pdf_path, batch_start, batch_end)
                fname = f"{stem}_p{batch_start+1}-{batch_end}.pdf"

            batch_prompt = prompt
            if n_batches > 1:
                batch_prompt += (
                    f"\n\nThis is batch {batch_idx+1} of {n_batches} "
                    f"(pages {batch_start+1}-{batch_end} of {n_pages}). "
                    f"Continue from where the previous batch ended. "
                    f"Do not repeat content from previous batches."
                )

            result_text, usage = _call_pdf_api(pdf_bytes, fname, model_id, batch_prompt)
            md_parts.append(result_text)

            for k in total_usage:
                total_usage[k] += usage[k]
            batch_cost = estimate_cost(usage["prompt_tokens"], usage["completion_tokens"], model_id)
            console.print(f"  │         [yellow]{usage['total_tokens']:,}[/yellow] tokens  💰 [green]{format_cost(batch_cost)}[/green]")

    else:
        # ── Image mode (fallback) ───────────────────────────────
        console.print(f"  ├ [blue]render:[/blue]  converting pages to images...")
        images = _render_pdf_pages(pdf_path)
        render_time = time.time() - start
        total_img_kb = sum(len(b) for b in images) * 3 // 4 // 1024
        console.print(f"  ├ [blue]render:[/blue]  [yellow]{len(images)}[/yellow] pages "
                      f"(~{total_img_kb} KB)  [dim]{render_time:.1f}s[/dim]")

        for batch_idx in range(n_batches):
            batch_start = batch_idx * effective_batch
            batch_end = min(batch_start + effective_batch, len(images))
            batch_images = images[batch_start:batch_end]
            batch_label = f"p.{batch_start+1}-{batch_end}"

            console.print(f"  ├ [blue]API:[/blue]     batch {batch_idx+1}/{n_batches} "
                          f"({batch_label}, {len(batch_images)} pages)...")

            batch_prompt = prompt
            if n_batches > 1:
                batch_prompt += (
                    f"\n\nThis is batch {batch_idx+1} of {n_batches} "
                    f"(pages {batch_start+1}-{batch_end} of {n_pages}). "
                    f"Continue from where the previous batch ended. "
                    f"Do not repeat content from previous batches."
                )

            result_text, usage = _call_image_api(batch_images, model_id, batch_prompt)
            md_parts.append(result_text)

            for k in total_usage:
                total_usage[k] += usage[k]
            batch_cost = estimate_cost(usage["prompt_tokens"], usage["completion_tokens"], model_id)
            console.print(f"  │         [yellow]{usage['total_tokens']:,}[/yellow] tokens  💰 [green]{format_cost(batch_cost)}[/green]")

    # ── Combine and save ─────────────────────────────────────────
    combined_md = "\n\n".join(md_parts)

    # Strip markdown code fences if model wrapped output
    combined_md = re.sub(r'^```(?:markdown|md)?\\n', '', combined_md)
    combined_md = re.sub(r'\\n```$', '', combined_md)

    # Run postprocess
    from convert import postprocess
    combined_md, pp_stats = postprocess(combined_md)

    part_out_dir.mkdir(parents=True, exist_ok=True)
    md_path = part_out_dir / f"{stem}.md"
    md_path.write_text(combined_md, encoding="utf-8")

    # Final stats
    out_size = md_path.stat().st_size
    out_lines = len(combined_md.splitlines())
    elapsed = time.time() - start

    total_cost = estimate_cost(total_usage["prompt_tokens"], total_usage["completion_tokens"], model_id)
    console.print(f"  ├ [blue]tokens:[/blue]  [yellow]{total_usage['prompt_tokens']:,}[/yellow] prompt + "
                  f"[yellow]{total_usage['completion_tokens']:,}[/yellow] completion = "
                  f"[yellow bold]{total_usage['total_tokens']:,}[/yellow bold]  "
                  f"💰 [green]{format_cost(total_cost)}[/green]")
    console.print(f"  └ [green]output:[/green]  [yellow]{out_lines}[/yellow] lines, "
                  f"[yellow]{_format_size(out_size)}[/yellow]  "
                  f"→ [dim]{md_path}[/dim]  [dim]{elapsed:.1f}s[/dim]")

    return {
        "name": pdf_path.name,
        "status": "ok",
        "md_path": str(md_path),
        "pages": n_pages,
        "images": 0,
        "code_blocks": pp_stats["code_blocks"],
        "headings": pp_stats["headings"],
        "out_size": out_size,
        "out_lines": out_lines,
        "time": elapsed,
        "usage": total_usage,
    }


# ── Interactive Enhance Flow ─────────────────────────────────────

def enhance_interactive():
    """Full interactive flow with step-based navigation (← Back supported).

    Steps:
      0: Selection mode (single/multi) — only if >1 source
      1: Select source(s)
      2: Enhancement mode
      3: Select model
      4: Confirm plan
      5: Execute enhancement
    """
    # Check API key upfront
    if not os.environ.get("OPENROUTER_API_KEY"):
        console.print("\n[red bold]✗ OPENROUTER_API_KEY not set.[/red bold]")
        console.print("  Export it first:  [cyan]export OPENROUTER_API_KEY=sk-or-...[/cyan]")
        return

    sources = scan_md_sources()

    if not sources:
        console.print(f"[red]No Markdown found in {MARKDOWN_DIR}/.[/red]")
        console.print("[dim]Run conversion first: make convert[/dim]")
        console.print(f"[dim]Or place .md files directly in {MARKDOWN_DIR}/[/dim]")
        return

    # Build option strings
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

    # State variables
    use_multi = False
    selected_list = None
    mode = None
    model_id = None
    model_name = None

    step = 0
    while True:
        # ── Step 0: Selection mode ───────────────────────────────
        if step == 0:
            if len(sources) <= 1:
                use_multi = False
                step = 1
                continue

            sel = select_menu("Selection mode:", [
                "Single select",
                "Multi select (Space to toggle)",
            ], show_back=True)

            if sel is None:
                return  # back to main menu
            use_multi = sel == 1
            step = 1
            continue

        # ── Step 1: Select source(s) ─────────────────────────────
        elif step == 1:
            if use_multi:
                indices = select_menu_multi("Select sources to enhance:", options)
                if not indices:
                    if len(sources) <= 1:
                        return  # only 1 source, back = main menu
                    step = 0; continue
                selected_list = [sources[i] for i in indices]
            else:
                idx = select_menu("Select source to enhance:", options, show_back=True)
                if idx is None:
                    if len(sources) <= 1:
                        return
                    step = 0; continue
                selected_list = [sources[idx]]

            step = 2
            continue

        # ── Step 2: Enhancement mode ─────────────────────────────
        elif step == 2:
            total_est_cleanup = sum(s["est_tokens_cleanup"] for s in selected_list)
            total_est_rewrite = sum(s["est_tokens_rewrite"] for s in selected_list)
            total_est_study = sum(s["est_tokens_study"] for s in selected_list)
            total_est_tutorial = sum(s["est_tokens_tutorial"] for s in selected_list)

            mode = select_mode(
                est_cleanup=total_est_cleanup,
                est_rewrite=total_est_rewrite,
                est_study=total_est_study,
                est_tutorial=total_est_tutorial,
            )
            if mode is None:
                step = 1; continue

            # Show mode info
            est_key = f"est_tokens_{mode}"
            total_est = sum(s[est_key] for s in selected_list)
            console.print(f"\n🔧 Mode [bold]{mode}[/bold] — {MODES[mode]}")
            console.print(f"📊 Estimated tokens: [yellow bold]~{total_est:,}[/yellow bold]")

            # Show cost range across all enhance models
            est_detail = estimate_tokens("x" * total_est * 4, mode)  # rough split
            costs = [estimate_cost(est_detail["prompt_tokens"], est_detail["completion_tokens"], m) for m in MODELS]
            if costs:
                lo, hi = min(costs), max(costs)
                console.print(f"💰 Estimated cost: [green]{format_cost(lo)}[/green] ~ [yellow]{format_cost(hi)}[/yellow]")

            step = 3
            continue

        # ── Step 3: Select model ─────────────────────────────────
        elif step == 3:
            # Calculate token estimates for cost display
            est_key_2 = f"est_tokens_{mode}"
            total_est_2 = sum(s[est_key_2] for s in selected_list)
            est_detail_2 = estimate_tokens("x" * total_est_2 * 4, mode)

            result = select_model(
                est_prompt=est_detail_2["prompt_tokens"],
                est_completion=est_detail_2["completion_tokens"],
            )
            if result is None:
                step = 2; continue
            model_id, model_name = result

            est_cost = estimate_cost(est_detail_2["prompt_tokens"], est_detail_2["completion_tokens"], model_id)
            console.print(f"  🤖 [cyan]{model_name}[/cyan]  💰 ~[yellow]{format_cost(est_cost)}[/yellow]")

            step = 4
            continue

        # ── Step 4: Confirm plan ─────────────────────────────────
        elif step == 4:
            total_files = sum(len(s["md_files"]) for s in selected_list)
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

            console.print()
            try:
                confirm = input("Proceed? [Y/n/b(ack)]: ").strip().lower()
                if confirm in ("b", "back"):
                    step = 3; continue
                if confirm and confirm not in ("y", "yes", ""):
                    console.print("[dim]Cancelled.[/dim]")
                    return
            except (EOFError, KeyboardInterrupt):
                console.print("[dim]Cancelled.[/dim]")
                return

            step = 5
            continue

        # ── Step 5: Execute enhancement ──────────────────────────
        elif step == 5:
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
            return