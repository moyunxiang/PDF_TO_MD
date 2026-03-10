#!/usr/bin/env python3
"""
PDF to Markdown — unified entry point.

Usage:
    python main.py              # Interactive main menu
    python main.py convert      # Convert all PDFs
    python main.py enhance      # Enhance MDs with API
    python main.py split FILE   # Split large PDF
"""

import sys
from pathlib import Path

from rich.panel import Panel

from convert import (
    console,
    select_menu,
    select_menu_multi,
    convert_single,
    print_summary,
    ask_ocr_mode,
    ask_perf_mode,
    find_pdfs,
    get_output_dir,
    _format_size,
    _scan_pdfs,
    PDF_DIR,
    MARKDOWN_DIR,
)


def _select_pdf(pdfs: list[Path]) -> Path | None:
    """Let user pick a PDF from the list with arrow keys."""
    options = []
    for p in pdfs:
        try:
            rel = p.relative_to(PDF_DIR)
        except ValueError:
            rel = p.name
        options.append(f"{rel}  ({_format_size(p.stat().st_size)})")
    idx = select_menu("Select PDF:", options, show_back=True)
    if idx is None:
        return None
    return pdfs[idx]


def _select_pdfs_multi(pdfs: list[Path]) -> list[Path] | None:
    """Let user pick multiple PDFs with Space/Tab to toggle."""
    options = []
    for p in pdfs:
        try:
            rel = p.relative_to(PDF_DIR)
        except ValueError:
            rel = p.name
        options.append(f"{rel}  ({_format_size(p.stat().st_size)})")
    indices = select_menu_multi("Select PDFs to convert:", options)
    if not indices:
        return None
    return [pdfs[i] for i in indices]


# ── Actions ──────────────────────────────────────────────────────

def do_convert():
    """Convert PDFs → Markdown with step-based navigation (← Back supported)."""
    import time
    pdf_items = find_pdfs(PDF_DIR)
    if not pdf_items:
        console.print(f"[red]No PDFs found in {PDF_DIR}/[/red]")
        return

    all_pdfs = [p for p, _ in pdf_items]
    items_map = {str(p): out for p, out in pdf_items}

    # State variables
    sel_mode = None   # 0=single, 1=multi, 2=all
    selected = None
    disable_ocr = None
    perf = None

    step = 0
    while True:
        # ── Step 0: Selection mode ───────────────────────────────
        if step == 0:
            if len(all_pdfs) <= 1:
                # Only 1 PDF, skip selection
                selected = all_pdfs
                step = 2
                continue

            sel_mode = select_menu("Selection mode:", [
                "Single select",
                "Multi select (Space to toggle)",
                "All",
            ], show_back=True)

            if sel_mode is None:
                return  # back to main menu

            if sel_mode == 2:  # All
                selected = all_pdfs
                step = 2
            else:
                step = 1
            continue

        # ── Step 1: Select PDF(s) ────────────────────────────────
        elif step == 1:
            if sel_mode == 0:
                picked = _select_pdf(all_pdfs)
                if picked is None:
                    step = 0; continue
                selected = [picked]
            else:
                picked = _select_pdfs_multi(all_pdfs)
                if picked is None:
                    step = 0; continue
                selected = picked

            step = 2
            continue

        # ── Step 2: Info panel + OCR mode ────────────────────────
        elif step == 2:
            scan = _scan_pdfs(selected)
            console.print(Panel(
                f"📚 [bold]{len(selected)}[/bold] PDF{'s' if len(selected) > 1 else ''} "
                f"({_format_size(scan['total_size'])})\n"
                f"📄 [yellow]{scan['total_pages']}[/yellow] pages total",
                title="[bold]PDF → Markdown[/bold]", border_style="blue",
            ))

            result = ask_ocr_mode(selected[0])
            if result is None:
                if len(all_pdfs) <= 1:
                    return  # only 1 PDF, back = main menu
                step = 0; continue
            disable_ocr = result
            step = 3
            continue

        # ── Step 3: Performance mode ─────────────────────────────
        elif step == 3:
            result = ask_perf_mode()
            if result is None:
                step = 2; continue
            perf = result
            step = 4
            continue

        # ── Step 4: Execute conversion ───────────────────────────
        elif step == 4:
            results = []
            total_start = time.time()
            for i, pdf in enumerate(selected, 1):
                out_dir = items_map.get(str(pdf), get_output_dir(pdf))
                out_dir.mkdir(parents=True, exist_ok=True)
                stats = convert_single(pdf, out_dir, index=i, total=len(selected),
                                       disable_ocr=disable_ocr, perf=perf)
                results.append(stats)

            print_summary(results, time.time() - total_start)
            console.print(f"\n[dim]💡 To enhance with API: make enhance[/dim]")
            return


def do_enhance():
    """Enhance existing MDs with API (independent step)."""
    from api import enhance_interactive
    enhance_interactive()


def do_split(pdf_name: str | None = None, pages: int | None = None):
    """Split a large PDF into smaller PDFs (no conversion)."""
    from split import split_pdf

    if pdf_name:
        pdf_path = Path(pdf_name)
        if not pdf_path.exists():
            pdf_path = PDF_DIR / pdf_name
    else:
        # Only show top-level PDFs for splitting (not already-split chapters)
        pdfs = sorted(PDF_DIR.glob("*.pdf"))
        if not pdfs:
            console.print(f"[red]No PDFs found in {PDF_DIR}/[/red]")
            return
        pdf_path = _select_pdf(pdfs)
        if pdf_path is None:
            return  # back to main menu

    if not pdf_path.exists():
        console.print(f"[red bold]Error:[/red bold] {pdf_path} not found")
        return

    split_pdf(pdf_path, pages_per_chunk=pages)


# ── Main Menu ────────────────────────────────────────────────────

ACTIONS = [
    ("Convert PDFs → Markdown",         do_convert),
    ("Split large PDF",                 lambda: do_split()),
    ("Enhance Markdown with API",       do_enhance),
]


def main():
    # Handle direct subcommand: python main.py convert / enhance / split
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "convert":
            do_convert()
            return
        elif cmd == "enhance":
            do_enhance()
            return
        elif cmd == "split":
            pdf_name = sys.argv[2] if len(sys.argv) > 2 else None
            # Don't treat flags as pdf_name
            if pdf_name and pdf_name.startswith("--"):
                pdf_name = None
            pages = None
            if "--pages" in sys.argv:
                pi = sys.argv.index("--pages")
                if pi + 1 < len(sys.argv):
                    pages = int(sys.argv[pi + 1])
            do_split(pdf_name, pages)
            return

    # Interactive main menu (loops until Escape)
    while True:
        pdf_items = find_pdfs(PDF_DIR)
        if pdf_items:
            all_pdfs = [p for p, _ in pdf_items]
            scan = _scan_pdfs(all_pdfs)
            console.print(Panel(
                f"📂 [bold]{len(all_pdfs)}[/bold] PDFs in [cyan]{PDF_DIR}/[/cyan] "
                f"({_format_size(scan['total_size'])})\n"
                f"📄 [yellow]{scan['total_pages']}[/yellow] pages total",
                title="[bold]PDF → Markdown[/bold]", border_style="blue",
            ))
        else:
            console.print(Panel(
                f"📂 [dim]No PDFs in {PDF_DIR}/[/dim]\n"
                f"[dim]Add PDF files to get started[/dim]",
                title="[bold]PDF → Markdown[/bold]", border_style="blue",
            ))

        console.print()
        options = [f"{i+1}) {name}" for i, (name, _) in enumerate(ACTIONS)]
        idx = select_menu("What to do?", options)
        if idx is None:
            console.print("[dim]Bye! 👋[/dim]")
            break

        console.print()
        _, action = ACTIONS[idx]
        action()
        console.print()  # spacing before next loop


if __name__ == "__main__":
    main()
