#!/bin/bash
# Build zfec to WebAssembly using Emscripten
# Prerequisites: Install emsdk and run `source emsdk_env.sh`

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$SCRIPT_DIR/../src"

echo "Building zfec WASM..."

# Check for emcc
if ! command -v emcc &> /dev/null; then
    echo "Error: emcc not found. Please install and activate emsdk:"
    echo "  git clone https://github.com/emscripten-core/emsdk.git"
    echo "  cd emsdk && ./emsdk install latest && ./emsdk activate latest"
    echo "  source emsdk_env.sh"
    exit 1
fi

# Create output directory
mkdir -p "$OUT_DIR"

# Compile fec.c to WASM with exported functions
emcc "$SCRIPT_DIR/fec.c" \
    -O3 \
    -s WASM=1 \
    -s MODULARIZE=1 \
    -s EXPORT_NAME="createFecModule" \
    -s EXPORTED_FUNCTIONS='["_fec_init","_fec_new","_fec_free","_fec_encode","_fec_decode","_malloc","_free"]' \
    -s EXPORTED_RUNTIME_METHODS='["ccall","cwrap","getValue","setValue","HEAPU8"]' \
    -s ALLOW_MEMORY_GROWTH=1 \
    -s INITIAL_MEMORY=2097152 \
    -s MAXIMUM_MEMORY=16777216 \
    -s NO_EXIT_RUNTIME=1 \
    -s ENVIRONMENT='web,worker' \
    -o "$OUT_DIR/fec.js"

echo "WASM build complete:"
echo "  $OUT_DIR/fec.js"
echo "  $OUT_DIR/fec.wasm"
