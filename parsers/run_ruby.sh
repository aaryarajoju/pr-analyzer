#!/bin/sh
# Run Ruby parser with clean output (suppresses Bundler warnings)
cd "$(dirname "$0")"
BUNDLE_GEMFILE="$(pwd)/Gemfile" bundle exec ruby ruby_parser.rb "$@"
