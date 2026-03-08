#!/usr/bin/env python3
"""
PDF to Markdown — unified entry point.

Usage:
    python main.py              # Interactive main menu
    python main.py convert      # Jump to convert
    python main.py split FILE   # Jump to split
    python main.py compare      # Jump to compare
"""

import sys
from pathlib import Path

from rich.panel import Panel

from convert import (
    console,
    select_menu,
    interactive_select,
    convert_single,
    convert_all,
    print_summary,
    _format_size,
    _scan_pdfs,
    MODES,
    PDF_DIR,
    OUTPUT_DIR,
)


def _select_pdf(pdfs: list[Path]) -> Path | None:
    """Let user pick a PDF from the list with arrow keys."""
    options = [f"{p.name}  ({_format_size(p.stat().st_size)})" for p in pdfs]
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
    """Convert all PDFs in pdf/."""
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        console.print(f"[red]No PDFs found in {PDF_DIR}/[/red]")
        return

    scan = _scan_pdfs(pdfs)
    _show_info_panel(pdfs, scan)

    mode, model_id, model_name = interactive_select(est_b=scan["est_b"], est_c=scan["est_c"])

    mode_line = f"\n🔧 Mode [bold]{mode}[/bold] — {MODES[mode]}"
    if model_name:
        mode_line += f"  [cyan]{model_name}[/cyan]"
    console.print(mode_line)

    est_tokens = scan["est_b"] if mode == "B" else scan["est_c"] if mode == "C" else 0
    convert_all(PDF_DIR, OUTPUT_DIR, mode, model_id, est_tokens)


def do_convert_single():
    """Convert a single PDF."""
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        console.print(f"[red]No PDFs found in {PDF_DIR}/[/red]")
        return

    pdf_path = _select_pdf(pdfs)
    if pdf_path is None:
        console.print("[dim]Cancelled.[/dim]")
        return

    scan = _scan_pdfs([pdf_path])

    console.print(Panel(
        f"📄 [cyan]{pdf_path.name}[/cyan] ({_format_size(scan['total_size'])})\n"
        f"📄 [yellow]{scan['total_pages']}[/yellow] pages",
        title="[bold]PDF → Markdown[/bold]", border_style="blue",
    ))

    mode, model_id, model_name = interactive_select(est_b=scan["est_b"], est_c=scan["est_c"])

    mode_line = f"\n🔧 Mode [bold]{mode}[/bold] — {MODES[mode]}"
    if model_name:
        mode_line += f"  [cyan]{model_name}[/cyan]"
    console.print(mode_line)

    est_tokens = scan["est_b"] if mode == "B" else scan["est_c"] if mode == "C" else 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stats = convert_single(pdf_path, OUTPUT_DIR, index=1, total=1)

    if mode in ("B", "C") and model_id:
        import time
        from api import enhance_file
        api_start = time.time()
        usage = enhance_file(Path(stats["md_path"]), mode, model_id)
        stats["api_usage"] = usage
        stats["api_time"] = time.time() - api_start
        stats["time"] += stats["api_time"]

    print_summary([stats], stats["time"], mode, est_tokens)


def do_split(pdf_name: str | None = None, pages: int | None = None):
    """Split a large PDF by chapters."""
    from split import split_and_convert

    if pdf_name:
        pdf_path = Path(pdf_name)
        if not pdf_path.exists():
            pdf_path = PDF_DIR / pdf_name
    else:
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

    split_and_convert(pdf_path, pages_per_chunk=pages)


def do_compare():
    """Compare output vs reference."""
    from compare import compare_all
    compare_all()


# ── Main Menu ────────────────────────────────────────────────────

ACTIONS = [
    ("Convert all PDFs",              do_convert_all),
    ("Convert single PDF",            do_convert_single),
    ("Split large PDF by chapters",   lambda: do_split()),
    ("Compare output vs reference",   do_compare),
]


def main():
    # Handle direct subcommand: python main.py convert / split / compare
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
                console.print(Panel(
                    f"📄 [cyan]{pdf_path.name}[/cyan] ({_format_size(scan['total_size'])})\n"
                    f"📄 [yellow]{scan['total_pages']}[/yellow] pages",
                    title="[bold]PDF → Markdown[/bold]", border_style="blue",
                ))
                mode, model_id, model_name = interactive_select(est_b=scan["est_b"], est_c=scan["est_c"])
                mode_line = f"\n🔧 Mode [bold]{mode}[/bold] — {MODES[mode]}"
                if model_name:
                    mode_line += f"  [cyan]{model_name}[/cyan]"
                console.print(mode_line)
                est_tokens = scan["est_b"] if mode == "B" else scan["est_c"] if mode == "C" else 0
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                stats = convert_single(pdf_path, OUTPUT_DIR, index=1, total=1)
                if mode in ("B", "C") and model_id:
                    import time
                    from api import enhance_file
                    api_start = time.time()
                    usage = enhance_file(Path(stats["md_path"]), mode, model_id)
                    stats["api_usage"] = usage
                    stats["api_time"] = time.time() - api_start
                    stats["time"] += stats["api_time"]
                print_summary([stats], stats["time"], mode, est_tokens)
            else:
                do_convert_all()
            return
        elif cmd == "split":
            pdf_name = sys.argv[2] if len(sys.argv) > 2 else None
            pages = None
            if "--pages" in sys.argv:
                pi = sys.argv.index("--pages")
                if pi + 1 < len(sys.argv):
                    pages = int(sys.argv[pi + 1])
            do_split(pdf_name, pages)
            return
        elif cmd == "compare":
            do_compare()
            return

    # Interactive main menu
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if pdfs:
        scan = _scan_pdfs(pdfs)
        _show_info_panel(pdfs, scan)
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
