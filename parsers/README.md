# Parsers

Standalone scripts to parse Ruby and TypeScript/TSX files.

## Setup

```bash
cd parsers

# Ruby: isolate gems to avoid Bundler warnings from other projects
bundle config set --local path 'vendor/bundle'
bundle install

# TypeScript
npm install
```

## Usage

```bash
# Via controller (recommended)
python3 controller.py path/to/file.rb
python3 controller.py path/to/file.tsx

# Direct
./run_ruby.sh path/to/file.rb
./run_ts.sh path/to/file.tsx

# Or with redirect (Bundler/Node warnings go to stderr, JSON to stdout)
bundle exec ruby ruby_parser.rb file.rb 2>/dev/null > output.json
npx tsx ts_parser.ts file.tsx 2>/dev/null > output.json
```

## Suppressing Warnings

- **Bundler**: Run `bundle config set --local path 'vendor/bundle'` before `bundle install` so gems are isolated.
- **Node**: Use `NODE_OPTIONS=--no-warnings` or the `run_ts.sh` script.
