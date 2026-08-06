# Build the pipeline walkthrough PDF.
#
# Requires TeXLive (or MacTeX) with the tikz + pgfplots packages.
# On macOS:  brew install --cask mactex-no-gui   (or the full mactex)
# On Ubuntu: apt install texlive-latex-recommended texlive-pictures \
#                        texlive-fonts-recommended texlive-science

TARGET := pipeline
LATEX  := pdflatex
LFLAGS := -interaction=nonstopmode -halt-on-error

.PHONY: all clean view

all: $(TARGET).pdf

$(TARGET).pdf: $(TARGET).tex
	$(LATEX) $(LFLAGS) $(TARGET).tex
	# Second pass so the ToC and cross-references resolve.
	$(LATEX) $(LFLAGS) $(TARGET).tex

view: $(TARGET).pdf
	open $(TARGET).pdf 2>/dev/null || xdg-open $(TARGET).pdf

clean:
	rm -f *.aux *.log *.out *.toc $(TARGET).pdf
