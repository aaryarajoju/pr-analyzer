#!/usr/bin/env ruby
# frozen_string_literal: true

# Parses a Ruby file using Prism and outputs JSON with classes, methods, and external class calls.
# Usage: ruby ruby_parser.rb <file_path>

require "prism"
require "json"

class RubyParser < Prism::Visitor
  attr_reader :classes, :instance_methods, :class_methods, :external_calls

  def initialize
    super()
    @classes = []
    @instance_methods = []
    @class_methods = []
    @external_calls = []
  end

  def visit_class_node(node)
    @classes << node.name.to_s
    super
  end

  def visit_module_node(node)
    @classes << node.name.to_s
    super
  end

  def visit_def_node(node)
    if node.receiver
      @class_methods << node.name.to_s
    else
      @instance_methods << node.name.to_s
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
  source = File.read(file_path)
  result = Prism.parse_file(file_path)

  unless result.success?
    warn result.errors_format
    exit 1
  end

  visitor = RubyParser.new
  result.value.accept(visitor)

  {
    classes: visitor.classes.uniq,
    instance_methods: visitor.instance_methods.uniq,
    class_methods: visitor.class_methods.uniq,
    external_calls: visitor.external_calls.uniq.sort
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
