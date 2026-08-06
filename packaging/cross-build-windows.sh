#!/usr/bin/env bash
# Cross-build the Windows NSIS installer from a Linux host.
#
# Uses the same PBS backend pipeline as build-desktop.sh's
# build_backend_windows(), but runs the Windows python.exe through wine so the
# whole thing can be produced on Linux. Then invokes electron-builder --win
# (NSIS) with wine available for its code-signing/icon steps.
#
# Requirements: uv (Windows PBS), wine, node/npm. Run from the repo root.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KC_VERSION="$(grep -m1 '__version__' "$ROOT/src/kiro_crew/__init__.py" \
  | sed -E 's/.*=[[:space:]]*"([^"]+)".*/\1/')"
if [ -z "$KC_VERSION" ]; then
  echo "ERROR: could not parse __version__" >&2
  exit 1
fi
echo "Building KiroCrew Windows installer for version $KC_VERSION"

ELECTRON_DIR="$ROOT/website/electron"

# --- 1. Frontend (reuse if already staged) ---------------------------------
if [ "${SKIP_FRONTEND:-0}" != "1" ]; then
  echo "==> Building dashboard (npm)…"
  ( cd "$ROOT/website"
    if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
    npm run build )
fi
[ -f "$ROOT/website/dist/index.html" ] || { echo "❌ dashboard dist missing" >&2; exit 1; }

# --- 2. Windows backend tree ------------------------------------------------
WINPY="$(find "$(uv python dir)" -maxdepth 1 -type d -name "cpython-3.12*-windows-x86_64-none" | sort -V | tail -1)"
[ -n "$WINPY" ] || { echo "❌ no Windows PBS; run: uv python install cpython-3.12-windows-x86_64-none" >&2; exit 1; }
echo "    Windows PBS: $WINPY"

OUT="$ELECTRON_DIR/backend-dist/kirocrew-backend"
rm -rf "$OUT"
mkdir -p "$(dirname "$OUT")"
cp -R "$WINPY" "$OUT"
find "$OUT" -name "EXTERNALLY-MANAGED" -delete 2>/dev/null || true

echo "==> pip-installing kiro_crew into Windows backend (via wine)…"
env PYTHONNOUSERSITE=1 PYTHONPATH= KIROCREW_SKIP_FRONTEND=1 \
  wine "$OUT/python.exe" -m pip install --prefer-binary \
  --no-warn-script-location --disable-pip-version-check "$ROOT"

# --- 3. Stage dashboard + stamp --------------------------------------------
SP="$OUT/Lib/site-packages"
echo "==> Staging dashboard dist…"
mkdir -p "$SP/kiro_crew/static"
( cd "$SP/kiro_crew/static" && rm -rf dist && cp -R "$ROOT/website/dist" dist )
[ -f "$SP/kiro_crew/static/dist/index.html" ] || { echo "❌ dist not staged" >&2; exit 1; }

bash "$ROOT/scripts/stamp-distribution.sh" "source" "$SP/kiro_crew"

# --- 4. Launcher shim + self-containment gate ------------------------------
mkdir -p "$OUT/bin"
printf '@echo off\r\n"%%~dp0..\\python.exe" -s -m kiro_crew %%*\r\n' > "$OUT/bin/kirocrew.cmd"

echo "==> Verifying self-containment (via wine)…"
PYTHONNOUSERSITE=1 wine "$OUT/python.exe" -s -m kiro_crew --version >/dev/null \
  || { echo "❌ bundled Windows backend is NOT self-contained" >&2; exit 1; }

# --- 5. Prune ---------------------------------------------------------------
( cd "$OUT"
  find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find Lib/site-packages -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + 2>/dev/null || true
  rm -rf Lib/test Lib/idlelib Lib/tkinter Lib/turtledemo Lib/ensurepip Lib/lib2to3 2>/dev/null || true
  # The uv Windows PBS tree ships a `python` symlink (created by uv's shim on
  # the host). On a Windows host that resolves to a .exe; on a Linux host it
  # dangles at an absolute uv path, which 7za refuses to archive into the NSIS
  # payload. Windows launches python.exe (via bin/kirocrew.cmd), so the bare
  # symlink is dead weight — drop it.
  find . -type l ! -exec test -e {} \; -delete 2>/dev/null || true )
echo "    backend size: $(du -sh "$OUT" 2>/dev/null | cut -f1)"

# --- 6. electron-builder --win ----------------------------------------------
echo "==> electron-builder --win (NSIS)…"
( cd "$ELECTRON_DIR"
  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
  rm -rf dist/win-unpacked dist/*.exe dist/*.yml dist/*.blockmap 2>/dev/null || true
  CSC_IDENTITY_AUTO_DISCOVERY=false \
    ./node_modules/.bin/electron-builder --win \
      -c.extraMetadata.version="$KC_VERSION" )

echo "==> Done. Installer:"
ls -1 "$ELECTRON_DIR/dist"/*.exe 2>/dev/null | sed 's/^/   /' || true
