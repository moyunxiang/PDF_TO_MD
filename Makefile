.PHONY: run convert convert-one split split-pages compare clean setup postprocess

VENV := .venv/bin/python

# Main entry point (recommended)
run:
	$(VENV) main.py

# Convert all PDFs — interactive menu
convert:
	$(VENV) main.py convert

# Convert a single PDF: make convert-one F=1.review-const.pdf
convert-one:
	$(VENV) main.py convert $(F)

# Split a large PDF by chapters: make split F=textbook.pdf
split:
	$(VENV) main.py split $(F)

# Split by page count: make split-pages F=textbook.pdf P=50
split-pages:
	$(VENV) main.py split $(F) --pages $(P)

# Re-run post-processing only (no re-conversion, mode A only)
postprocess:
	$(VENV) -c "from convert import postprocess_file; from pathlib import Path; [postprocess_file(f) or print(f'  ✓ {f}') for f in sorted(Path('output').glob('*/*.md'))]"

# Compare output/ vs md_ref/
compare:
	$(VENV) main.py compare

# Clean output
clean:
	rm -rf output/

# Setup environment
setup:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
