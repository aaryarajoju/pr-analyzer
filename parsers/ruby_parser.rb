#!/usr/bin/env ruby
# frozen_string_literal: true

# Parses a Ruby file using Prism and outputs JSON with classes, methods, external class calls,
# plus LoD (Law of Demeter) chain lengths and CMO (Class Method Overuse) ratio.
#
# Extended for the hybrid static + LLM pipeline (see NEW-DESIGN.md).
# Backward-compatible: original fields (classes, instance_methods, class_methods, external_calls)
# are still present; new fields (lod_chain_lengths, cmo_ratio, class_method_count,
# instance_method_count) are added.
#
# Usage: ruby ruby_parser.rb <file_path>

require "prism"
require "json"

# LoD chain-length helper: scan source lines for chained method calls (2+ dots)
module TextMetricsHelper
  module_function

  def call_chain_lengths(source)
    return [] unless source

    source.lines.flat_map do |line|
      line.scan(/(?:[a-zA-Z_]\w*\.){2,}[a-zA-Z_]\w*/).map { |m| m.count(".") + 1 }
    end
  end
end

class RubyParser < Prism::Visitor
  attr_reader :classes, :instance_methods, :class_methods, :external_calls,
              :lod_chain_lengths, :per_class_stats

  def initialize
    super()
    @classes            = []
    @instance_methods   = []
    @class_methods      = []
    @external_calls     = []
    @lod_chain_lengths  = []   # all chain lengths across all methods/classes
    @per_class_stats    = {}   # class_name => { instance_methods: N, class_methods: N }
    @current_class_name = nil
    @source             = nil
  end

  # Called by parse_file before visiting, to give access to raw source
  def attach_source(src)
    @source = src
  end

  def visit_class_node(node)
    name = node.name.to_s
    @classes << name
    prev = @current_class_name
    @current_class_name = name
    @per_class_stats[name] ||= { "instance_methods" => 0, "class_methods" => 0 }
    super
    @current_class_name = prev
  end

  def visit_module_node(node)
    name = node.name.to_s
    @classes << name
    prev = @current_class_name
    @current_class_name = name
    @per_class_stats[name] ||= { "instance_methods" => 0, "class_methods" => 0 }
    super
    @current_class_name = prev
  end

  def visit_def_node(node)
    if node.receiver
      @class_methods << node.name.to_s
      if @current_class_name && @per_class_stats[@current_class_name]
        @per_class_stats[@current_class_name]["class_methods"] += 1
      end
    else
      @instance_methods << node.name.to_s
      if @current_class_name && @per_class_stats[@current_class_name]
        @per_class_stats[@current_class_name]["instance_methods"] += 1
      end
    end

    # Collect LoD chain lengths from this method body
    if @source
      start_off = node.location.start_offset
      end_off   = node.location.end_offset
      body_src  = @source[start_off...end_off] || ""
      chains    = TextMetricsHelper.call_chain_lengths(body_src)
      @lod_chain_lengths.concat(chains)
    end

    super
  end

  def visit_call_node(node)
    return super unless node.receiver

    receiver = node.receiver
    case receiver
    when Prism::ConstantReadNode
      @external_calls << receiver.name.to_s
    when Prism::ConstantPathNode
      @external_calls << receiver.full_name
    end
    super
  end
end

def parse_file(file_path)
  source = File.read(file_path, encoding: "utf-8", invalid: :replace, undef: :replace)
  result = Prism.parse(source)

  unless result.success?
    warn result.errors_format
    exit 1
  end

  visitor = RubyParser.new
  visitor.attach_source(source)
  result.value.accept(visitor)

  total_class_methods    = visitor.class_methods.length
  total_instance_methods = visitor.instance_methods.length
  total_methods          = total_class_methods + total_instance_methods
  cmo_ratio              = total_methods.positive? ? (total_class_methods.to_f / total_methods).round(3) : 0.0

  max_chain = visitor.lod_chain_lengths.max || 0

  {
    # ── Original fields (backward-compatible) ────────────────────────────────
    classes:         visitor.classes.uniq,
    instance_methods: visitor.instance_methods.uniq,
    class_methods:   visitor.class_methods.uniq,
    external_calls:  visitor.external_calls.uniq.sort,

    # ── New fields for hybrid pipeline ───────────────────────────────────────
    lod_chain_lengths:      visitor.lod_chain_lengths,          # all chain depths
    lod_max_chain:          max_chain,                           # highest chain depth found
    lod_violations_count:   visitor.lod_chain_lengths.count { |d| d >= 3 },
    class_method_count:     total_class_methods,
    instance_method_count:  total_instance_methods,
    cmo_ratio:              cmo_ratio,
    per_class_stats:        visitor.per_class_stats
  }
end

if __FILE__ == $PROGRAM_NAME
  file_path = ARGV[0]
  if file_path.nil? || file_path.empty?
    warn "Usage: ruby ruby_parser.rb <file_path>"
    exit 1
  end

  unless File.exist?(file_path)
    warn "File not found: #{file_path}"
    exit 1
  end

  output = parse_file(file_path)
  puts JSON.pretty_generate(output)
end
