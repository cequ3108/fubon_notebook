#!/usr/bin/env bash
# Download Fubon Neo SDK v2.2.8, test environment certs, and sample code.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SDK_DIR="$ROOT/.sdk"
mkdir -p "$SDK_DIR"

curl -fsSL -o "$SDK_DIR/fubon_neo_linux.zip" \
  "https://www.fbs.com.tw/TradeAPI_SDK/fubon_binary/fubon_neo-2.2.8-cp37-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.zip"
curl -fsSL -o "$SDK_DIR/test_environment.zip" \
  "https://www.fbs.com.tw/TradeAPI_SDK/sample_code/test_environment.zip"
curl -fsSL -o "$SDK_DIR/python_sample_code.zip" \
  "https://www.fbs.com.tw/TradeAPI_SDK/sample_code/python_sample_code.zip"

unzip -q -o "$SDK_DIR/fubon_neo_linux.zip" -d "$SDK_DIR/fubon_neo"
unzip -q -o "$SDK_DIR/test_environment.zip" -d "$SDK_DIR/test_env"
unzip -q -o "$SDK_DIR/python_sample_code.zip" -d "$SDK_DIR/sample_code"

WHEEL=$(ls "$SDK_DIR/fubon_neo"/*.whl | head -1)
pip3 install --user -q "$WHEEL"
echo "Installed $(basename "$WHEEL")"
echo "Test certs: $SDK_DIR/test_env/test_environment/*.pfx"
