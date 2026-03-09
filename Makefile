.PHONY: run convert convert-one enhance split split-pages clean setup postprocess

VENV := .venv/bin/python

# Main entry point (recommended)
run:
	$(VENV) main.py

# Convert all PDFs (pure marker, no API)
convert:
	$(VENV) main.py convert

# Convert a single PDF: make convert-one F=1.review-const.pdf
convert-one:
	$(VENV) main.py convert $(F)

# Enhance converted MDs with API (independent step)
enhance:
	$(VENV) main.py enhance

# Split a large PDF: make split F=textbook.pdf [W=3]
split:
	$(VENV) main.py split $(F) $(if $(W),--workers $(W))

# Split by page count: make split-pages F=textbook.pdf P=50 [W=3]
split-pages:
	$(VENV) main.py split $(F) --pages $(P) $(if $(W),--workers $(W))

# Re-run post-processing only (no re-conversion)
postprocess:
	$(VENV) -c "from convert import postprocess_file; from pathlib import Path; [postprocess_file(f) or print(f'  ✓ {f}') for f in sorted(Path('markdown').glob('*/*.md'))]"

# Clean output
clean:
	rm -rf markdown/ enhanced/

# Setup environment
setup:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
