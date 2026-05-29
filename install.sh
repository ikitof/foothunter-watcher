#!/usr/bin/env bash
# Installe l'icône "Foot Live" dans le menu d'applications (ou un dossier choisi).
#
#   ./install.sh                 # -> menu d'applications (~/.local/share/applications)
#   ./install.sh ~/Bureau        # -> icône cliquable sur le bureau
#   ./install.sh ~/Desktop
#
# Le chemin du script est détecté automatiquement : aucun chemin en dur.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/.local/share/applications}"
mkdir -p "$DEST"

TARGET="$DEST/foot-live.desktop"
sed "s#@APPDIR@#$DIR#g" "$DIR/foot-live.desktop" > "$TARGET"
chmod +x "$TARGET"

# Sur le bureau, autoriser le double-clic si l'outil est dispo (GNOME).
if command -v gio >/dev/null 2>&1; then
    gio set "$TARGET" metadata::trusted true 2>/dev/null || true
fi

echo "Installé : $TARGET"
