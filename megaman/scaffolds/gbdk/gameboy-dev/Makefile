# GBDK-2020 Makefile — builds src/*.c into build/game.gb
#
# Needs gbdk-2020: https://github.com/gbdk-2020/gbdk-2020/releases
# Unpack to ~/gbdk (or set GBDK_HOME). `make` fails with a clear message if missing.

GBDK_HOME ?= $(HOME)/gbdk
LCC = $(GBDK_HOME)/bin/lcc

# LCC flags:  -Wa-l -Wl-m -Wl-j  → listing + map + no$sym files for debugging
# For MBC/banking, add e.g.: -Wl-yt0x1B -Wl-yo4 -Wl-ya1  (MBC5, 4 ROM banks, 1 RAM bank)
LCCFLAGS = -Wa-l -Wl-m -Wl-j

PROJECT = game
SRC     = $(wildcard src/*.c)
BUILD   = build
ROM     = $(BUILD)/$(PROJECT).gb

all: check-toolchain $(ROM)

check-toolchain:
	@test -x $(LCC) || { \
	  echo "ERROR: gbdk-2020 not found at $(GBDK_HOME)."; \
	  echo "Install: download a release from github.com/gbdk-2020/gbdk-2020,"; \
	  echo "unpack to ~/gbdk, or run: make GBDK_HOME=/path/to/gbdk"; \
	  exit 1; }

$(ROM): $(SRC)
	mkdir -p $(BUILD)
	$(LCC) $(LCCFLAGS) -o $(ROM) $(SRC)

size: $(ROM)
	@ls -l $(ROM) | awk '{printf "ROM: %d bytes (%.1f KB of 32 KB base)\n", $$5, $$5/1024}'
	@grep -c "BANK" $(BUILD)/*.map 2>/dev/null || true

clean:
	rm -rf $(BUILD)

.PHONY: all check-toolchain size clean
