#!/usr/bin/env bash
#
# install_dependencies.sh
# ─────────────────────────
# Installs the external bioinformatics binaries GEPER's core pipeline
# depends on: bwa, minimap2, samtools, freebayes, bcftools.
#
# This script does NOT touch any GEPER pipeline code, Python package, or
# configuration file — it only installs system-level binaries.
#
# Supported paths (auto-detected, in this order):
#   1. apt (Ubuntu/Debian)      — bwa, samtools, bcftools, minimap2 via apt;
#                                  freebayes built from source (not reliably
#                                  packaged on all Ubuntu releases).
#   2. conda/mamba (if present) — all five tools via bioconda, on any OS.
#   3. Homebrew (macOS)         — bwa, samtools, bcftools, minimap2, freebayes.
#
# Usage:
#   chmod +x install_dependencies.sh
#   ./install_dependencies.sh              # auto-detect package manager
#   ./install_dependencies.sh --conda       # force conda/bioconda path
#   ./install_dependencies.sh --apt         # force apt path
#   ./install_dependencies.sh --brew        # force Homebrew path
#
# After running, verify with:
#   python main.py verify-environment
#
set -euo pipefail

BOLD="$(tput bold 2>/dev/null || true)"
RESET="$(tput sgr0 2>/dev/null || true)"

log()  { echo "${BOLD}[install_dependencies]${RESET} $*"; }
fail() { echo "${BOLD}[install_dependencies] ERROR:${RESET} $*" >&2; exit 1; }

FORCE_MODE=""
for arg in "$@"; do
  case "$arg" in
    --conda) FORCE_MODE="conda" ;;
    --apt)   FORCE_MODE="apt" ;;
    --brew)  FORCE_MODE="brew" ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^#//'
      exit 0
      ;;
    *) fail "Unknown argument: $arg (use --conda, --apt, --brew, or no argument to auto-detect)" ;;
  esac
done

detect_mode() {
  if [ -n "$FORCE_MODE" ]; then
    echo "$FORCE_MODE"
    return
  fi
  if command -v conda >/dev/null 2>&1 || command -v mamba >/dev/null 2>&1; then
    echo "conda"
  elif command -v apt-get >/dev/null 2>&1; then
    echo "apt"
  elif command -v brew >/dev/null 2>&1; then
    echo "brew"
  else
    echo "unknown"
  fi
}

install_via_conda() {
  local CONDA_BIN
  CONDA_BIN="$(command -v mamba || command -v conda)"
  log "Using ${CONDA_BIN} (bioconda) to install bwa, samtools, bcftools, freebayes, minimap2 ..."
  "$CONDA_BIN" install -y -c bioconda -c conda-forge \
      bwa samtools bcftools freebayes minimap2
}

install_via_apt() {
  log "Using apt to install bwa, samtools, bcftools, minimap2 ..."
  sudo apt-get update
  sudo apt-get install -y \
      bwa samtools bcftools minimap2 \
      build-essential cmake zlib1g-dev libbz2-dev liblzma-dev \
      libcurl4-openssl-dev libssl-dev libncurses5-dev pkg-config git

  if command -v freebayes >/dev/null 2>&1; then
    log "freebayes already present, skipping source build."
  else
    log "freebayes is not reliably packaged for all Ubuntu releases — building from source ..."
    local BUILD_DIR
    BUILD_DIR="$(mktemp -d)"
    git clone --recursive https://github.com/freebayes/freebayes.git "$BUILD_DIR/freebayes"
    (
      cd "$BUILD_DIR/freebayes"
      make -j"$(nproc)"
      sudo cp bin/freebayes /usr/local/bin/
    )
    rm -rf "$BUILD_DIR"
  fi
}

install_via_brew() {
  log "Using Homebrew to install bwa, samtools, bcftools, minimap2, freebayes ..."
  brew update
  brew install bwa samtools bcftools minimap2 || true
  if ! brew install freebayes; then
    log "Homebrew freebayes formula unavailable — falling back to conda/bioconda."
    if command -v conda >/dev/null 2>&1; then
      conda install -y -c bioconda -c conda-forge freebayes
    else
      fail "freebayes could not be installed via Homebrew and conda is not available. " \
           "Install Miniconda and re-run with --conda, or see docs/INSTALL_DEPENDENCIES.md."
    fi
  fi
}

MODE="$(detect_mode)"
log "Detected installation mode: ${MODE}"

case "$MODE" in
  conda) install_via_conda ;;
  apt)   install_via_apt ;;
  brew)  install_via_brew ;;
  *)
    fail "No supported package manager found (conda/mamba, apt-get, or brew). " \
         "See docs/INSTALL_DEPENDENCIES.md for manual installation instructions, " \
         "including Docker and WSL2 for Windows."
    ;;
esac

echo
log "Done. Verifying installed tools ..."
for tool in bwa samtools bcftools freebayes minimap2; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "  ✓ $tool found at $(command -v "$tool")"
  else
    echo "  ✗ $tool NOT found"
  fi
done

echo
log "Run 'python main.py verify-environment' for the full GEPER environment report."
