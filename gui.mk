# Build Makefile for the YT-DLP GUI (gui.py)
#
# Usage:
#   make -f gui.mk            # build the executable
#   make -f gui.mk clean      # remove build artifacts
#   make -f gui.mk run        # run the built executable
#
# Output: dist/YouTube Downloader.exe

# Pin to the Python 3.13 install where PyInstaller is installed.
# Override with: make -f gui.mk PYTHON="path\to\python.exe"
PYTHON      ?= C:/Users/denni/AppData/Local/Programs/Python/Python313/python.exe
PYINSTALLER ?= $(PYTHON) -m PyInstaller

APP_NAME    := YouTube Downloader
ENTRY       := gui.py
ICON        := icon.ico
DIST_DIR    := dist
BUILD_DIR   := build
SPEC_FILE   := $(APP_NAME).spec

PYI_FLAGS := \
	--noconfirm \
	--onefile \
	--windowed \
	--clean \
	--name "$(APP_NAME)" \
	--icon "$(ICON)" \
	--collect-all yt_dlp \
	--add-data "icon.ico;." \
	--distpath "$(DIST_DIR)" \
	--workpath "$(BUILD_DIR)"

.PHONY: all build clean run rebuild

all: build

# Phony target — spaces in the output filename break make's filename rules,
# so we let PyInstaller decide whether a rebuild is needed.
# USERPROFILE/HOME are exported so PyInstaller can resolve Path.home() under msys make.
build:
	USERPROFILE="$${USERPROFILE:-C:/Users/$$USERNAME}" \
	HOME="$${HOME:-$$USERPROFILE}" \
	$(PYINSTALLER) $(PYI_FLAGS) "$(ENTRY)"

rebuild: clean build

run: build
	"$(DIST_DIR)/$(APP_NAME).exe"

clean:
	rm -rf "$(BUILD_DIR)" "$(DIST_DIR)" "$(SPEC_FILE)"
