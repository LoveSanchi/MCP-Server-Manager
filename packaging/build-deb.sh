#!/usr/bin/env bash
# Build dist/mcp-keeper_<version>_all.deb from this checkout (no debhelper needed).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); import mcp_keeper; print(mcp_keeper.__version__)")"
STAGE="$ROOT/build/mcp-keeper_${VERSION}_all"
PKG="$ROOT/packaging"

rm -rf "$STAGE"
install -d "$STAGE/DEBIAN" "$STAGE/usr/lib/mcp-keeper" "$STAGE/usr/bin" \
  "$STAGE/usr/lib/systemd/user" "$STAGE/etc/xdg/autostart" "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/doc/mcp-keeper"

sed "s/@VERSION@/$VERSION/" "$PKG/debian/control" > "$STAGE/DEBIAN/control"
install -m 755 "$PKG/debian/postinst" "$PKG/debian/prerm" "$PKG/debian/postrm" "$STAGE/DEBIAN/"

( cd "$ROOT" && find mcp_keeper -type f \( -name '*.py' -o -name '*.svg' \) -print0 ) |
  while IFS= read -r -d '' f; do
    mode=644; [[ "$f" == *.sh ]] && mode=755
    install -D -m "$mode" "$ROOT/$f" "$STAGE/usr/lib/mcp-keeper/$f"
  done

install -m 755 "$PKG/bin/mcp-keeperd" "$STAGE/usr/lib/mcp-keeper/mcp-keeperd"
install -m 755 "$PKG/bin/mcp-keeper" "$PKG/bin/mcp-keeper-shim" "$PKG/bin/mcp-keeper-indicator" "$STAGE/usr/bin/"
install -m 644 "$PKG/mcp-keeper.service" "$STAGE/usr/lib/systemd/user/"
install -m 644 "$PKG/mcp-keeper-indicator.desktop" "$STAGE/etc/xdg/autostart/"
install -m 644 "$PKG/mcp-keeper-indicator.desktop" "$STAGE/usr/share/applications/"
install -m 644 "$ROOT/README.md" "$ROOT/LICENSE" "$STAGE/usr/share/doc/mcp-keeper/"

mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$ROOT/dist/mcp-keeper_${VERSION}_all.deb"
