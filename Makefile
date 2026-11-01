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

.PHONY: all clean
