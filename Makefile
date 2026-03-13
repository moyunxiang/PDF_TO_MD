.PHONY: run convert enhance split split-pages pricing clean setup postprocess

VENV := .venv/bin/python

# Main entry point (recommended)
run:
	$(VENV) main.py

# Convert all PDFs (pure marker, no API) — auto-detects split subdirectories
convert:
	$(VENV) main.py convert

# Enhance converted MDs with API (independent step)
enhance:
	$(VENV) main.py enhance

# Split a large PDF (no conversion): make split F=textbook.pdf → pdf/textbook/
split:
	$(VENV) main.py split $(F)

# Split by page count: make split-pages F=textbook.pdf P=50
split-pages:
	$(VENV) main.py split $(F) --pages $(P)


# Update model pricing from OpenRouter (free, no API key needed)
pricing:
	$(VENV) main.py pricing

# Re-run post-processing only (no re-conversion)
postprocess:
	$(VENV) -c "from convert import postprocess_file; from pathlib import Path; [postprocess_file(f) or print(f'  ✓ {f}') for f in sorted(Path('markdown').glob('**/*.md'))]"

# Clean output
clean:
	rm -rf markdown/ enhanced/

# Setup environment
setup:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
