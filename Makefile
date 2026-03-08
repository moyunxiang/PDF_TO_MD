.PHONY: convert convert-one compare clean setup postprocess

VENV := .venv/bin/python

# Convert PDFs — interactive menu for mode/model selection
convert:
	$(VENV) convert.py

# Convert a single PDF: make convert-one F=1.review-const.pdf
convert-one:
	$(VENV) convert.py $(F)

# Re-run post-processing only (no re-conversion, mode A only)
postprocess:
	$(VENV) -c "from convert import postprocess_file; from pathlib import Path; [postprocess_file(f) or print(f'  ✓ {f}') for f in sorted(Path('output').glob('*/*.md'))]"

# Compare output/ vs md_ref/
compare:
	$(VENV) compare.py

# Clean output
clean:
	rm -rf output/

# Setup environment
setup:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
