#!/bin/bash
set -e

echo "=== Building Tinta4PlusU ==="

echo "[1/2] Building GUI (tinta4plusu)..."
pyinstaller tinta4plusu.spec --noconfirm

echo "[2/2] Building Helper Daemon (tinta4plusu-helper)..."
pyinstaller tinta4plusu-helper.spec --noconfirm

echo ""
echo "=== Build complete ==="
echo "GUI binary:    dist/tinta4plusu/tinta4plusu"
echo "Helper binary: dist/tinta4plusu-helper/tinta4plusu-helper"
