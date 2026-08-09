# Build the ADS-B walkthrough PDFs.
#
# Requires TeXLive (or MacTeX) with the tikz + pgfplots packages.
# On macOS:  brew install --cask mactex-no-gui   (or the full mactex)
# On Ubuntu: apt install texlive-latex-recommended texlive-pictures \
#                        texlive-fonts-recommended texlive-science

TARGETS := pipeline range
LATEX   := pdflatex
LFLAGS  := -interaction=nonstopmode -halt-on-error

PDFS := $(addsuffix .pdf, $(TARGETS))

.PHONY: all clean view $(TARGETS)

all: $(PDFS)

# Individual per-target rules (each runs pdflatex twice for cross-refs)
%.pdf: %.tex
	$(LATEX) $(LFLAGS) $<
	$(LATEX) $(LFLAGS) $<

# Convenience: `make pipeline` / `make range`
$(TARGETS): %: %.pdf

view: $(PDFS)
	@for p in $(PDFS); do open $$p 2>/dev/null || xdg-open $$p; done

clean:
	rm -f *.aux *.log *.out *.toc $(PDFS)
