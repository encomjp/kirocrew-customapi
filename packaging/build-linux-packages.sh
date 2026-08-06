#!/bin/bash
# Build KiroCrew-customapi Linux packages: .deb, .rpm, relocatable tarball.
#
# Produces (in dist/packages/):
#   kirocrew-customapi_<ver>_amd64.deb
#   kirocrew-customapi-<ver>-1.x86_64.rpm
#   kirocrew-customapi-<ver>-linux-x86_64.tar.gz
#
# Each package bundles a self-contained Python venv (/opt/kirocrew-customapi)
# with ALL dependencies — no pip install at the consumer side. The Claude
# Code backend (claude + claude-agent-acp via npm) is intentionally NOT
# bundled; it is an optional runtime for provider=claude_code.
#
# Requirements: python3, dpkg-deb, rpmbuild (rpm-tools), tar.
# The venv is built with the SYSTEM python (>= 3.10) so pyvenv.cfg points
# at /usr/bin — the package is NOT relocatable across python versions.
#
# Usage: bash packaging/build-linux-packages.sh [version]
#   version defaults to the version in pyproject.toml

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(grep -E '^version' "$REPO_ROOT/pyproject.toml" | head -1 | sed -E 's/version = "(.*)"/\1/')}"
PKG="kirocrew-customapi"
STAGE="$(mktemp -d)/stage"
DIST="$REPO_ROOT/dist/packages"
mkdir -p "$STAGE/opt/$PKG" "$DIST"

echo "==> Building $PKG $VERSION (venv with system python)"
# Prefer a REAL system python over uv/venv-managed interpreters: pip writes
# absolute interpreter paths into bin/ scripts, and a uv-managed python would
# bake a machine-local path into every package.
SYS_PY="$(command -v /usr/bin/python3 || command -v python3)"
PYTHON_BIN="$(readlink -f "$SYS_PY")"
echo "    using $PYTHON_BIN"
"$PYTHON_BIN" -m venv "$STAGE/opt/$PKG/venv"
"$STAGE/opt/$PKG/venv/bin/pip" install --quiet --upgrade pip
"$STAGE/opt/$PKG/venv/bin/pip" install --quiet "$REPO_ROOT"

# Sanity: the bundled CLI must run (before the shebang rewrite)
"$STAGE/opt/$PKG/venv/bin/kirocrew" --version >/dev/null

# Rewrite every bin/ shebang to the FIXED target path so the package is
# relocatable (works from /opt in .deb/.rpm and any --prefix in the tarball).
for f in "$STAGE/opt/$PKG/venv/bin"/*; do
    [ -f "$f" ] || continue
    if head -1 "$f" | grep -q '^#!'; then
        sed -i "1s|^#!.*|#!/opt/$PKG/venv/bin/python|" "$f"
    fi
done

# venv points at the python that created it; force system python links so
# pyvenv.cfg is /usr/bin-based.
rm -f "$STAGE/opt/$PKG/venv/bin/python" "$STAGE/opt/$PKG/venv/bin/python3"
ln -s "$PYTHON_BIN" "$STAGE/opt/$PKG/venv/bin/python"
ln -s "$PYTHON_BIN" "$STAGE/opt/$PKG/venv/bin/python3"
sed -i -E "s|^home = .*|home = $(dirname "$PYTHON_BIN")|" "$STAGE/opt/$PKG/venv/pyvenv.cfg"
sed -i -E "s|^executable = .*|executable = $PYTHON_BIN|" "$STAGE/opt/$PKG/venv/pyvenv.cfg"

# ---- shared payload -------------------------------------------------------
mkdir -p "$STAGE/usr/bin" "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/512x512/apps"
cat > "$STAGE/usr/bin/kirocrew" <<'EOF'
#!/bin/sh
exec /opt/kirocrew-customapi/venv/bin/kirocrew "$@"
EOF
chmod 755 "$STAGE/usr/bin/kirocrew"

cat > "$STAGE/usr/share/applications/kirocrew-customapi.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Kiro Crew
GenericName=AI Development Workspace
Comment=Persistent development workspace with self-hosted model routing
Exec=kirocrew chat
Icon=kirocrew
Terminal=true
Categories=Development;Utility;
Keywords=ai;agent;coding;assistant;crew;
StartupWMClass=KiroCrew
EOF

if [ -f "$REPO_ROOT/assets/kirocrew.png" ]; then
    cp "$REPO_ROOT/assets/kirocrew.png" "$STAGE/usr/share/icons/hicolor/512x512/apps/"
elif [ -f "$HOME/.local/share/icons/hicolor/512x512/apps/kirocrew.png" ]; then
    cp "$HOME/.local/share/icons/hicolor/512x512/apps/kirocrew.png" \
       "$STAGE/usr/share/icons/hicolor/512x512/apps/"
fi

# ---- .deb ----------------------------------------------------------------
DEB_DIR="$STAGE/DEBIAN"
mkdir -p "$DEB_DIR"
cat > "$DEB_DIR/control" <<EOF
Package: $PKG
Version: $VERSION-1
Section: utils
Priority: optional
Architecture: amd64
Depends: python3 (>= 3.10)
Recommends: nodejs, npm
Maintainer: Adrian Kozlowski <encomjp@users.noreply.github.com>
Homepage: https://github.com/encomjp/KiroCrew-customapi
Description: Kiro Crew with the Claude Code ACP backend for custom LLM routers
 Kiro Crew is an open source development workspace that runs locally or
 remotely on your hardware. This fork re-enables the dormant claude_code
 provider so you can drive Kiro Crew through your own model router
 (e.g. a local CLIProxyAPI or 9router instance speaking the Anthropic API) instead of
 Kiro's built-in Bedrock catalog - no Kiro account required.
 .
 Bundled: self-contained Python venv with all dependencies. The Claude
 Code backend (claude + claude-agent-acp via npm) is optional but
 required for provider=claude_code.
EOF
for f in postinst postrm; do
    cat > "$DEB_DIR/$f" <<'EOF'
#!/bin/sh
set -e
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
fi
exit 0
EOF
    chmod 755 "$DEB_DIR/$f"
done
dpkg-deb --build --root-owner-group "$STAGE" "$DIST/${PKG}_${VERSION}-1_amd64.deb" >/dev/null
echo "==> .deb:   $DIST/${PKG}_${VERSION}-1_amd64.deb"

# ---- tarball -------------------------------------------------------------
TARBALL="$(mktemp -d)"
mkdir -p "$TARBALL/kirocrew-customapi-$VERSION"
cp -a "$STAGE/opt/$PKG/venv" "$TARBALL/kirocrew-customapi-$VERSION/"
cat > "$TARBALL/kirocrew-customapi-$VERSION/install.sh" <<'EOF'
#!/bin/sh
# KiroCrew-customapi installer - relocatable tarball install
# Usage: ./install.sh [--prefix /opt/kirocrew-customapi] [--user]
set -e
PREFIX="/opt/kirocrew-customapi"
MODE="system"
case "$1" in
  --user) PREFIX="$HOME/.local/share/kirocrew-customapi"; MODE="user" ;;
  --prefix) PREFIX="$2"; shift ;;
esac
echo "==> Installing KiroCrew-customapi to $PREFIX"
mkdir -p "$PREFIX"
cp -a venv "$PREFIX/"
# Rewrite shebangs to the actual install prefix (the tarball is relocatable)
for f in "$PREFIX/venv/bin"/*; do
    [ -f "$f" ] || continue
    if head -1 "$f" | grep -q '^#!'; then
        sed -i "1s|^#!.*|#!$PREFIX/venv/bin/python|" "$f"
    fi
done
ln -sf "$PREFIX/venv/bin/kirocrew" "$PREFIX/kirocrew"
if [ "$MODE" = "user" ]; then
    mkdir -p "$HOME/.local/bin"
    ln -sf "$PREFIX/venv/bin/kirocrew" "$HOME/.local/bin/kirocrew"
    echo "==> Symlink: $HOME/.local/bin/kirocrew (ensure ~/.local/bin is on PATH)"
else
    if [ "$(id -u)" = "0" ]; then
        ln -sf "$PREFIX/venv/bin/kirocrew" /usr/local/bin/kirocrew
        echo "==> Symlink: /usr/local/bin/kirocrew"
    else
        echo "==> WARNING: not root - run 'sudo ln -sf $PREFIX/venv/bin/kirocrew /usr/local/bin/kirocrew'"
    fi
fi
echo
echo "==> Done. Verify with: kirocrew --version"
echo "    Optional backend for provider=claude_code:"
echo "    npm install -g @anthropic-ai/claude-code @agentclientprotocol/claude-agent-acp"
EOF
chmod 755 "$TARBALL/kirocrew-customapi-$VERSION/install.sh"
tar -czf "$DIST/kirocrew-customapi-$VERSION-linux-x86_64.tar.gz" \
    -C "$TARBALL" "kirocrew-customapi-$VERSION"
echo "==> tarball: $DIST/kirocrew-customapi-$VERSION-linux-x86_64.tar.gz"

# ---- .rpm ----------------------------------------------------------------
RPMTOP="$(mktemp -d)"
mkdir -p "$RPMTOP"/{BUILD,BUILDROOT,RPMS,SOURCES,SRPMS,SPECS} "$RPMTOP/rpmdb"
RPMTARBALL="$RPMTOP/SOURCES/$PKG-$VERSION.tar.gz"
TARBALL2="$(mktemp -d)"
mkdir -p "$TARBALL2/kirocrew-customapi-$VERSION/opt"
cp -a "$STAGE/opt/$PKG" "$TARBALL2/kirocrew-customapi-$VERSION/opt/"
cp "$STAGE/usr/share/applications/kirocrew-customapi.desktop" \
   "$STAGE/usr/share/icons/hicolor/512x512/apps/kirocrew.png" \
   "$TARBALL2/kirocrew-customapi-$VERSION/" 2>/dev/null || true
tar -czf "$RPMTARBALL" -C "$TARBALL2" "kirocrew-customapi-$VERSION"

cat > "$RPMTOP/SPECS/$PKG.spec" <<EOF
Name:           $PKG
Version:        $VERSION
Release:        1
Summary:        Kiro Crew with Claude Code ACP backend for custom LLM routers
License:        Apache-2.0
URL:            https://github.com/encomjp/KiroCrew-customapi
Source0:        %{name}-%{version}.tar.gz
BuildArch:      x86_64
Requires:       python3 >= 3.10
Recommends:     nodejs, npm

%description
Kiro Crew is an open source development workspace that runs locally or
remotely on your hardware. This fork re-enables the dormant claude_code
provider so you can drive Kiro Crew through your own model router
(e.g. a local CLIProxyAPI or 9router instance speaking the Anthropic API) instead of
Kiro's built-in Bedrock catalog - no Kiro account required.

Bundled: self-contained Python venv with all dependencies. The Claude
Code backend (claude + claude-agent-acp via npm) is optional but
required for provider=claude_code.

%prep
%setup -q

%build
# payload is a pre-built relocatable venv

%install
mkdir -p %{buildroot}/opt/kirocrew-customapi
mkdir -p %{buildroot}/usr/bin
mkdir -p %{buildroot}/usr/share/applications
mkdir -p %{buildroot}/usr/share/icons/hicolor/512x512/apps
cp -a opt/kirocrew-customapi/venv %{buildroot}/opt/kirocrew-customapi/
cat > %{buildroot}/usr/bin/kirocrew <<'INNEREOF'
#!/bin/sh
exec /opt/kirocrew-customapi/venv/bin/kirocrew "\$@"
INNEREOF
chmod 755 %{buildroot}/usr/bin/kirocrew
cp kirocrew-customapi.desktop %{buildroot}/usr/share/applications/
cp kirocrew.png %{buildroot}/usr/share/icons/hicolor/512x512/apps/

%post
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
fi

%files
/opt/kirocrew-customapi/
/usr/bin/kirocrew
/usr/share/applications/kirocrew-customapi.desktop
/usr/share/icons/hicolor/512x512/apps/kirocrew.png

%changelog
* $(date -u '+%a %b %d %Y') Adrian Kozlowski <encomjp@users.noreply.github.com> - $VERSION-1
- Initial package: KiroCrew-customapi with bundled venv
EOF

rpmbuild --define "_topdir $RPMTOP" --define "_dbpath $RPMTOP/rpmdb" \
    -bb "$RPMTOP/SPECS/$PKG.spec" >/dev/null 2>&1
cp "$RPMTOP"/RPMS/x86_64/$PKG-$VERSION-1.x86_64.rpm "$DIST/"
echo "==> .rpm:   $DIST/$PKG-$VERSION-1.x86_64.rpm"

rm -rf "$STAGE" "$TARBALL" "$TARBALL2" "$RPMTOP"
echo
echo "==> All packages in $DIST:"
ls -la "$DIST"
