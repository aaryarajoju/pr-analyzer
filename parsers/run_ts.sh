#!/bin/sh
# Run TypeScript parser with clean output (suppresses Node warnings)
cd "$(dirname "$0")"
NODE_OPTIONS="--no-warnings" npx tsx ts_parser.ts "$@"
