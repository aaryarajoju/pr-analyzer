#!/usr/bin/env ruby
# frozen_string_literal: true

# Static analyzer using Prism AST. Detects all 10 design principles:
# SRP, OCP, LSP, DIP, ISP, LoD, DRY, Information Expert, Encapsulation, CMO
#
# Usage: bundle exec ruby static_analyzer.rb <file.rb> [<file.rb> ...]
#        bundle exec ruby static_analyzer.rb --stdin

require "prism"
require "json"
require "digest"

# Thresholds
LOD_MAX_CHAIN       = 3     # call-chain depth that triggers an LoD violation
SRP_MAX_METHODS     = 7     # method count threshold
SRP_MAX_INITS       = 5     # direct .new() instantiations threshold
CMO_MIN_CLASS_METHS = 3     # minimum class methods before CMO is evaluated
CMO_RATIO_THRESHOLD = 0.5   # class_methods / total_methods ratio
DRY_MIN_BODY_CHARS  = 30    # minimum body length before DRY dedup runs
DRY_MIN_DUPLICATES  = 2     # minimum duplicates to report
OCP_MAX_CONDITIONALS  = 1   # type-checking conditionals before OCP violation
DIP_MAX_CONCRETIONS   = 2   # max direct .new() calls (excl. interface-named classes)
ISP_MAX_METHODS       = 6   # modules with more methods than this violate ISP
ENC_MAX_ACCESSORS     = 3   # max attr_accessor macros before Encapsulation violation
ENC_MAX_PUBLIC_RATIO  = 0.85 # max ratio of public/total methods
ENC_MIN_METHODS       = 5   # minimum methods before public ratio check applies
IE_MIN_EXTERNAL_CALLS = 4   # minimum external calls before InformationExpert activates
IE_TOLERANCE          = 1   # allowed excess of external over ivar calls

# Data models
MethodRecord = Struct.new(
  :name, :class_method, :line, :arity, :body, :structural_hash, :call_chain_lengths, :visibility,
  keyword_init: true
)

ClassRecord = Struct.new(
  :name, :file, :type, :superclass, :methods, :source,
  :attr_reader_count, :attr_writer_count, :attr_accessor_count,
  keyword_init: true
) do
  def instance_methods_list = methods.reject(&:class_method)
  def class_methods_list    = methods.select(&:class_method)
  def instance_method_count = instance_methods_list.length
  def class_method_count    = class_methods_list.length
  def total_methods         = methods.length
  def public_method_count   = methods.count { |m| m.visibility == :public }

  def instantiation_counts
    counts = Hash.new(0)
    methods.each { |m| TextMetrics.instantiated_classes(m.body).each { |c, n| counts[c] += n } }
    counts
  end
end

# Text-metric helpers
module TextMetrics
  module_function

  # Chain depths for all chained calls in source (e.g. a.b.c → 3)
  def call_chain_lengths(source)
    return [] unless source

    source.lines.flat_map do |line|
      line.scan(/(?:[a-z_A-Z]\w*\.){2,}[a-z_A-Z]\w*/).map { |m| m.count(".") + 1 }
    end
  end

  # Unique class names from ClassName.new calls
  def instantiations(source)
    return [] unless source

    source.scan(/\b([A-Z]\w*(?:::\w+)*)\.new\b/).flatten.uniq
  end

  # Normalised SHA1 hash of method body (for DRY clone detection)
  def structural_hash(body)
    Digest::SHA1.hexdigest(body.gsub(/\s+/, " ").strip)
  end

  # Count .is_a?/.instance_of? calls and case-on-constant patterns (OCP)
  def conditional_dispatch_count(source)
    return 0 unless source

    source.scan(/\.(?:is_a\?|instance_of\?)/).length +
      source.scan(/case\s+[A-Z]/).length
  end

  # Count attr_reader/writer/accessor macros in class source
  def attr_macro_counts(source)
    return { attr_reader: 0, attr_writer: 0, attr_accessor: 0 } unless source

    {
      attr_reader:   source.scan(/\battr_reader\b/).length,
      attr_writer:   source.scan(/\battr_writer\b/).length,
      attr_accessor: source.scan(/\battr_accessor\b/).length
    }
  end

  # Count ivar accesses vs external method calls in a method body
  def information_distribution(source)
    return { ivar: 0, external: 0 } unless source

    {
      ivar:     source.scan(/@[\w]+/).length,
      external: source.scan(/\b[a-z_]\w*\.[a-z_]\w*/).length
    }
  end

  # Hash of { ClassName => count } for all ClassName.new calls
  def instantiated_classes(source)
    return {} unless source

    counts = Hash.new(0)
    source.scan(/\b([A-Z]\w*(?:::\w+)*)\.new\b/).flatten.each { |c| counts[c] += 1 }
    counts
  end
end

# Prism AST visitor — builds ClassRecord / MethodRecord objects
class AnalysisVisitor < Prism::Visitor
  attr_reader :class_records

  def initialize(file_path, source)
    super()
    @file_path        = file_path
    @source           = source
    @class_records    = []
    @class_stack      = []
    @namespace_stack  = []
    @visibility_stack = []
  end

  def visit_class_node(node)
    push_scope(node, :class) { super }
  end

  def visit_module_node(node)
    push_scope(node, :module) { super }
  end

  # Track standalone visibility modifiers (private/protected/public)
  def visit_call_node(node)
    if !@class_stack.empty? && node.receiver.nil? && node.arguments.nil?
      case node.name
      when :private   then @visibility_stack[-1] = :private
      when :protected then @visibility_stack[-1] = :protected
      when :public    then @visibility_stack[-1] = :public
      end
    end
    super
  end

  def visit_def_node(node)
    return super if @class_stack.empty?

    is_class_method = !node.receiver.nil? # def self.foo or def obj.foo
    record_method(node, class_method: is_class_method)
    super
  end

  private

  def push_scope(node, type, &block)
    name       = extract_name(node)
    qualified  = qualify(name)
    superclass = extract_superclass(node)
    cls_source = slice_source(node)
    attr_counts = TextMetrics.attr_macro_counts(cls_source)

    scope = { name: qualified, type: type, superclass: superclass, methods: [] }

    @namespace_stack.push(qualified)
    @class_stack.push(scope)
    @visibility_stack.push(:public)

    yield

    @class_stack.pop
    @namespace_stack.pop
    @visibility_stack.pop

    @class_records << ClassRecord.new(
      name:                scope[:name],
      file:                @file_path,
      type:                scope[:type],
      superclass:          scope[:superclass],
      methods:             scope[:methods],
      source:              cls_source,
      attr_reader_count:   attr_counts[:attr_reader],
      attr_writer_count:   attr_counts[:attr_writer],
      attr_accessor_count: attr_counts[:attr_accessor]
    )
  end

  def record_method(node, class_method:)
    body_src = slice_source(node)
    arity    = compute_arity(node)

    method_rec = MethodRecord.new(
      name:               node.name.to_s,
      class_method:       class_method,
      line:               node.location.start_line,
      arity:              arity,
      body:               body_src,
      structural_hash:    TextMetrics.structural_hash(body_src),
      call_chain_lengths: TextMetrics.call_chain_lengths(body_src),
      visibility:         @visibility_stack.last || :public
    )

    @class_stack.last[:methods] << method_rec
  end

  def qualify(name)
    return name if @namespace_stack.empty?

    (@namespace_stack + [name]).join("::")
  end

  def extract_name(node)
    path = node.respond_to?(:constant_path) ? node.constant_path : nil
    return node.name.to_s if node.respond_to?(:name) && !node.name.nil?
    return const_path_to_s(path) if path

    "AnonymousClass"
  rescue StandardError
    "AnonymousClass"
  end

  def extract_superclass(node)
    return nil unless node.respond_to?(:superclass) && node.superclass

    const_path_to_s(node.superclass)
  rescue StandardError
    nil
  end

  def const_path_to_s(node)
    return nil unless node

    case node
    when Prism::ConstantReadNode      then node.name.to_s
    when Prism::ConstantPathNode      then "#{const_path_to_s(node.parent)}::#{node.name}"
    when Prism::ConstantPathWriteNode then "#{const_path_to_s(node.target)}"
    else                                   node.respond_to?(:name) ? node.name.to_s : nil
    end
  rescue StandardError
    nil
  end

  def slice_source(node)
    start_off = node.location.start_offset
    end_off   = node.location.end_offset
    @source[start_off...end_off] || ""
  rescue StandardError
    ""
  end

  def compute_arity(node)
    return 0 unless node.respond_to?(:parameters) && node.parameters

    params = node.parameters
    count  = 0
    count += params.requireds.length  if params.respond_to?(:requireds)  && params.requireds
    count += params.optionals.length  if params.respond_to?(:optionals)  && params.optionals
    count
  rescue StandardError
    0
  end
end

# Detectors — one method per principle
module Detectors
  module_function

  def lod_violations(class_records)
    violations = []

    class_records.each do |cr|
      cr.methods.each do |method|
        method.call_chain_lengths.each do |depth|
          next if depth < LOD_MAX_CHAIN

          violations << {
            "file"        => cr.file,
            "class_name"  => cr.name,
            "method_name" => method.name,
            "line"        => method.line,
            "chain_depth" => depth,
            "description" => "Method `#{method.name}` in `#{cr.name}` has a call chain of depth #{depth} (max #{LOD_MAX_CHAIN - 1})."
          }
        end
      end
    end

    violations
  end

  def cmo_violations(class_records)
    violations = []

    class_records.each do |cr|
      next if cr.type == :module

      cm    = cr.class_method_count
      im    = cr.instance_method_count
      total = cm + im
      next if cm < CMO_MIN_CLASS_METHS
      next if total.zero?

      ratio = cm.to_f / total
      next if ratio < CMO_RATIO_THRESHOLD

      violations << {
        "file"                  => cr.file,
        "class_name"            => cr.name,
        "class_method_count"    => cm,
        "instance_method_count" => im,
        "ratio"                 => ratio.round(3),
        "description"           => "Class `#{cr.name}` has #{cm} class methods and #{im} instance methods (ratio #{ratio.round(2)}); consider converting to instance methods."
      }
    end

    violations
  end

  def srp_signals(class_records)
    signals = []

    class_records.each do |cr|
      next if cr.type == :module

      method_count   = cr.total_methods
      instantiations = cr.methods.flat_map { |m| TextMetrics.instantiations(m.body) }.uniq
      external_count = instantiations.length

      next if method_count < SRP_MAX_METHODS && external_count < SRP_MAX_INITS

      description =
        if method_count >= SRP_MAX_METHODS && external_count >= SRP_MAX_INITS
          "Class `#{cr.name}` has #{method_count} methods AND #{external_count} direct instantiations – likely doing too much."
        elsif method_count >= SRP_MAX_METHODS
          "Class `#{cr.name}` has #{method_count} methods (limit #{SRP_MAX_METHODS}); may violate SRP."
        else
          "Class `#{cr.name}` directly instantiates #{external_count} collaborators (limit #{SRP_MAX_INITS}); consider dependency injection."
        end

      signals << {
        "file"            => cr.file,
        "class_name"      => cr.name,
        "method_count"    => method_count,
        "external_count"  => external_count,
        "description"     => description
      }
    end

    signals
  end

  def dry_violations(class_records)
    buckets = Hash.new { |h, k| h[k] = [] }

    class_records.each do |cr|
      cr.methods.each do |method|
        next if method.body.strip.length < DRY_MIN_BODY_CHARS

        buckets[method.structural_hash] << {
          "class_name"  => cr.name,
          "method_name" => method.name,
          "file"        => cr.file,
          "line"        => method.line
        }
      end
    end

    violations = []
    buckets.each_value do |entries|
      next if entries.length < DRY_MIN_DUPLICATES

      dup_count = entries.length
      entries.each do |entry|
        violations << entry.merge(
          "duplicate_count" => dup_count - 1,
          "description"     => "Method `#{entry['method_name']}` in `#{entry['class_name']}` appears to duplicate #{dup_count - 1} other implementation(s)."
        )
      end
    end

    violations
  end

  def lsp_signals(class_records, class_index)
    signals = []

    class_records.each do |cr|
      next unless cr.superclass

      parent = class_index[cr.superclass]
      next unless parent

      parent_arities = parent.methods.each_with_object({}) do |m, h|
        h[m.name] = m.arity
      end

      cr.methods.each do |method|
        expected = parent_arities[method.name]
        next if expected.nil? || expected == method.arity

        signals << {
          "file"         => cr.file,
          "class_name"   => cr.name,
          "parent_class" => cr.superclass,
          "method_name"  => method.name,
          "child_arity"  => method.arity,
          "parent_arity" => expected,
          "description"  => "Method `#{method.name}` in `#{cr.name}` overrides parent `#{cr.superclass}` with arity #{method.arity} (parent expects #{expected})."
        }
      end
    end

    signals
  end

  def ocp_violations(class_records)
    violations = []

    class_records.each do |cr|
      count = TextMetrics.conditional_dispatch_count(cr.source)
      next if count <= OCP_MAX_CONDITIONALS

      violations << {
        "file"                       => cr.file,
        "class_name"                 => cr.name,
        "conditional_dispatch_count" => count,
        "description"                => "Class `#{cr.name}` has #{count} type-checking conditionals " \
                                        "(is_a?, instance_of?, case on constants) – may violate OCP."
      }
    end

    violations
  end

  def dip_violations(class_records)
    violations = []

    class_records.each do |cr|
      next if cr.type == :module

      concretions = cr.instantiation_counts.reject { |const, _| const.start_with?("I") }
      count = concretions.values.sum
      next if count <= DIP_MAX_CONCRETIONS

      violations << {
        "file"             => cr.file,
        "class_name"       => cr.name,
        "concretion_count" => count,
        "concretions"      => concretions,
        "description"      => "Class `#{cr.name}` directly instantiates #{count} concrete classes " \
                              "(max #{DIP_MAX_CONCRETIONS}); inject dependencies instead."
      }
    end

    violations
  end

  def isp_violations(class_records)
    violations = []

    class_records.each do |cr|
      next unless cr.type == :module

      count = cr.total_methods
      next if count <= ISP_MAX_METHODS

      violations << {
        "file"         => cr.file,
        "module_name"  => cr.name,
        "method_count" => count,
        "description"  => "Module `#{cr.name}` has #{count} methods (max #{ISP_MAX_METHODS}); " \
                          "split into focused interfaces."
      }
    end

    violations
  end

  def encapsulation_violations(class_records)
    violations = []

    class_records.each do |cr|
      next if cr.type == :module

      acc_count = cr.attr_accessor_count.to_i
      total     = cr.total_methods
      public_m  = cr.public_method_count

      attr_violation  = acc_count > ENC_MAX_ACCESSORS
      ratio_violation = total >= ENC_MIN_METHODS && public_m.to_f / total > ENC_MAX_PUBLIC_RATIO

      next unless attr_violation || ratio_violation

      reason =
        if attr_violation && ratio_violation
          "#{acc_count} attr_accessor macros and public ratio #{(public_m.to_f / total).round(2)}"
        elsif attr_violation
          "#{acc_count} attr_accessor macros (max #{ENC_MAX_ACCESSORS})"
        else
          "public method ratio #{(public_m.to_f / total).round(2)} (max #{ENC_MAX_PUBLIC_RATIO})"
        end

      violations << {
        "file"                => cr.file,
        "class_name"          => cr.name,
        "attr_accessor_count" => acc_count,
        "public_method_count" => public_m,
        "total_methods"       => total,
        "description"         => "Class `#{cr.name}` has poor encapsulation: #{reason}."
      }
    end

    violations
  end

  def information_expert_violations(class_records)
    violations = []

    class_records.each do |cr|
      cr.methods.each do |method|
        dist     = TextMetrics.information_distribution(method.body)
        ivar     = dist[:ivar]
        external = dist[:external]

        next if external < IE_MIN_EXTERNAL_CALLS
        next unless external > ivar + IE_TOLERANCE

        violations << {
          "file"           => cr.file,
          "class_name"     => cr.name,
          "method_name"    => method.name,
          "line"           => method.line,
          "ivar_accesses"  => ivar,
          "external_calls" => external,
          "description"    => "Method `#{method.name}` in `#{cr.name}` makes #{external} external " \
                              "calls vs #{ivar} ivar accesses; data may belong elsewhere."
        }
      end
    end

    violations
  end
end

# Runs all detectors and returns combined JSON-ready hash
def analyze_files(file_paths)
  all_records = []
  parse_errors = []

  file_paths.each do |path|
    unless File.exist?(path)
      parse_errors << { file: path, error: "File not found" }
      next
    end

    source = File.read(path, encoding: "utf-8", invalid: :replace, undef: :replace)
    result = Prism.parse(source)

    unless result.success?
      msg = result.errors.map(&:message).join("; ")
      parse_errors << { file: path, error: msg }
      next
    end

    visitor = AnalysisVisitor.new(path, source)
    result.value.accept(visitor)
    all_records.concat(visitor.class_records)
  rescue StandardError => e
    parse_errors << { file: path, error: e.message }
  end

  class_index = all_records.each_with_object({}) { |cr, h| h[cr.name] = cr }

  srp_sigs  = Detectors.srp_signals(all_records)
  ocp_viols = Detectors.ocp_violations(all_records)
  lsp_sigs  = Detectors.lsp_signals(all_records, class_index)
  dip_viols = Detectors.dip_violations(all_records)
  isp_viols = Detectors.isp_violations(all_records)
  lod_viols = Detectors.lod_violations(all_records)
  dry_viols = Detectors.dry_violations(all_records)
  ie_viols  = Detectors.information_expert_violations(all_records)
  enc_viols = Detectors.encapsulation_violations(all_records)
  cmo_viols = Detectors.cmo_violations(all_records)

  output = {
    "srp"                   => { "signals"    => srp_sigs,  "count" => srp_sigs.length  },
    "ocp"                   => { "violations" => ocp_viols, "count" => ocp_viols.length },
    "lsp"                   => { "signals"    => lsp_sigs,  "count" => lsp_sigs.length  },
    "dip"                   => { "violations" => dip_viols, "count" => dip_viols.length },
    "isp"                   => { "violations" => isp_viols, "count" => isp_viols.length },
    "law_of_demeter"        => { "violations" => lod_viols, "count" => lod_viols.length },
    "dry"                   => { "violations" => dry_viols, "count" => dry_viols.length },
    "information_expert"    => { "violations" => ie_viols,  "count" => ie_viols.length  },
    "encapsulation"         => { "violations" => enc_viols, "count" => enc_viols.length },
    "overuse_class_methods" => { "violations" => cmo_viols, "count" => cmo_viols.length }
  }

  output["parse_errors"] = parse_errors unless parse_errors.empty?
  output
end

if __FILE__ == $PROGRAM_NAME
  args = ARGV.dup

  if args.empty?
    warn "Usage: bundle exec ruby static_analyzer.rb <file.rb> [<file.rb> ...]"
    warn "       bundle exec ruby static_analyzer.rb --stdin   (read paths from stdin)"
    exit 1
  end

  file_paths =
    if args == ["--stdin"]
      $stdin.read.lines.map(&:strip).reject(&:empty?)
    else
      args
    end

  result = analyze_files(file_paths)
  puts JSON.pretty_generate(result)
end
