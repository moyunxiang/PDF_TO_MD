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
    convert_single,
    convert_all,
    print_summary,
    ask_ocr_mode,
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
    idx = select_menu("Select PDF:", options)
    if idx is None:
        return None
    return pdfs[idx]


def _show_info_panel(pdfs: list[Path], scan: dict):
    """Display the info panel with PDF stats."""
    info = (f"📂 [bold]{len(pdfs)}[/bold] PDFs in [cyan]{PDF_DIR}/[/cyan] "
            f"({_format_size(scan['total_size'])})\n"
            f"📄 [yellow]{scan['total_pages']}[/yellow] pages total")
    console.print(Panel(info, title="[bold]PDF → Markdown[/bold]", border_style="blue"))


# ── Actions ──────────────────────────────────────────────────────

def do_convert_all():
    """Convert all PDFs in pdf/ (including split subdirectories)."""
    pdf_items = find_pdfs(PDF_DIR)
    if not pdf_items:
        console.print(f"[red]No PDFs found in {PDF_DIR}/[/red]")
        return

    all_pdfs = [p for p, _ in pdf_items]
    scan = _scan_pdfs(all_pdfs)
    _show_info_panel(all_pdfs, scan)

    # Ask OCR mode (use first PDF for auto-detect sampling)
    disable_ocr = ask_ocr_mode(all_pdfs[0])

    convert_all(PDF_DIR, disable_ocr=disable_ocr)
    console.print(f"\n[dim]💡 To enhance with API: make enhance[/dim]")


def do_convert_single():
    """Convert a single PDF."""
    pdf_items = find_pdfs(PDF_DIR)
    if not pdf_items:
        console.print(f"[red]No PDFs found in {PDF_DIR}/[/red]")
        return

    all_pdfs = [p for p, _ in pdf_items]
    pdf_path = _select_pdf(all_pdfs)
    if pdf_path is None:
        console.print("[dim]Cancelled.[/dim]")
        return

    scan = _scan_pdfs([pdf_path])
    try:
        rel = pdf_path.relative_to(PDF_DIR)
    except ValueError:
        rel = pdf_path.name
    console.print(Panel(
        f"📄 [cyan]{rel}[/cyan] ({_format_size(scan['total_size'])})\n"
        f"📄 [yellow]{scan['total_pages']}[/yellow] pages",
        title="[bold]PDF → Markdown[/bold]", border_style="blue",
    ))

    # Ask OCR mode
    disable_ocr = ask_ocr_mode(pdf_path)

    out_dir = get_output_dir(pdf_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = convert_single(pdf_path, out_dir, index=1, total=1, disable_ocr=disable_ocr)
    print_summary([stats], stats["time"])
    console.print(f"\n[dim]💡 To enhance with API: make enhance[/dim]")


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
            console.print("[dim]Cancelled.[/dim]")
            return

    if not pdf_path.exists():
        console.print(f"[red bold]Error:[/red bold] {pdf_path} not found")
        return

    split_pdf(pdf_path, pages_per_chunk=pages)


# ── Main Menu ────────────────────────────────────────────────────

ACTIONS = [
    ("Convert PDFs → Markdown",         do_convert_all),
    ("Convert single PDF",              do_convert_single),
    ("Split large PDF",                 lambda: do_split()),
    ("Enhance Markdown with API",       do_enhance),
]


def main():
    # Handle direct subcommand: python main.py convert / enhance / split
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "convert":
            if len(sys.argv) > 2:
                # Single file shortcut
                pdf_name = sys.argv[2]
                pdf_path = Path(pdf_name)
                if not pdf_path.exists():
                    pdf_path = PDF_DIR / pdf_name
                if not pdf_path.exists():
                    console.print(f"[red bold]Error:[/red bold] {pdf_name} not found")
                    sys.exit(1)
                scan = _scan_pdfs([pdf_path])
                try:
                    rel = pdf_path.relative_to(PDF_DIR)
                except ValueError:
                    rel = pdf_path.name
                console.print(Panel(
                    f"📄 [cyan]{rel}[/cyan] ({_format_size(scan['total_size'])})\n"
                    f"📄 [yellow]{scan['total_pages']}[/yellow] pages",
                    title="[bold]PDF → Markdown[/bold]", border_style="blue",
                ))
                disable_ocr = ask_ocr_mode(pdf_path)
                out_dir = get_output_dir(pdf_path)
                out_dir.mkdir(parents=True, exist_ok=True)
                stats = convert_single(pdf_path, out_dir, index=1, total=1, disable_ocr=disable_ocr)
                print_summary([stats], stats["time"])
                console.print(f"\n[dim]💡 To enhance with API: make enhance[/dim]")
            else:
                do_convert_all()
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

    # Interactive main menu
    pdf_items = find_pdfs(PDF_DIR)
    if pdf_items:
        all_pdfs = [p for p, _ in pdf_items]
        scan = _scan_pdfs(all_pdfs)
        _show_info_panel(all_pdfs, scan)
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
        console.print("[dim]Cancelled.[/dim]")
        sys.exit(0)

    console.print()
    _, action = ACTIONS[idx]
    action()


if __name__ == "__main__":
    main()
