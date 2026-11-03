# M38 build: the C accelerator is optional by design.
#
# The package runs correctly without it (the Python aggregate path stays the reference), so a
# machine without a compiler loses speed, never answers. Building needs a C compiler and the
# interpreter's headers — a declared toolchain addition (docs/benchmark-spec.md's portability
# clause), not a silent one.

PYTHON ?= python3
CC ?= cc
CFLAGS ?= -O3 -fPIC -Wall -Wextra

INC := $(shell $(PYTHON) -c "import sysconfig; print(sysconfig.get_paths()['include'])")
EXT := $(shell $(PYTHON) -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))")

TARGET := minidb/_native$(EXT)

all: $(TARGET)

$(TARGET): native/rowaggregate.c
	$(CC) $(CFLAGS) -I$(INC) -shared -o $@ $<

clean:
	rm -f $(TARGET)

# Goal 2: one command that answers "is this tree good?" -- builds the optional accelerator,
# runs tests, corpus, the seeded crash sweep and the docs examples, and prints one summary
# block with raw counts and the git revision. Works in a fresh clone (no artifacts assumed).
verify:
	$(PYTHON) tools/verify.py

.PHONY: all clean verify
