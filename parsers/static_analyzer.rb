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
LOD_FOREIGN_DEPTH   = 2     # LoD: min object boundaries (depth >= 3) when root is foreign
LONG_CHAIN_MIN_DEPTH = 5    # long_chain: any chain with >= 5 elements
SRP_MAX_METHODS     = 7     # method count threshold (models, non-controllers)
SRP_CONTROLLER_MAX_METHODS = 12  # controllers: 7 REST actions + extras is normal
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

# New detectors (god_object, feature_envy, long_method, shotgun_surgery, ocp)
GOD_OBJECT_MAX_METHODS   = 15
GOD_OBJECT_MAX_IVARS    = 10
GOD_OBJECT_MAX_INITS    = 8
FEATURE_ENVY_MIN_EXTERNAL = 6
FEATURE_ENVY_RATIO      = 2.0
FEATURE_ENVY_CONTROLLER_MIN_EXTERNAL = 10
FEATURE_ENVY_CONTROLLER_RATIO       = 3.0
INFO_EXPERT_MIN_EXTERNAL = 8
INFO_EXPERT_RATIO       = 3.0
INFO_EXPERT_CONTROLLER_MIN_EXTERNAL = 12
INFO_EXPERT_CONTROLLER_RATIO       = 4.0
LONG_METHOD_MAX_LINES   = 20
SHOTGUN_SURGERY_MIN_EXTERNAL_CLASSES = 8
OCP_MIN_BRANCHES        = 4
OCP_TYPE_CHECK_MIN     = 2

# Data models
MethodRecord = Struct.new(
  :name, :class_method, :line, :arity, :body, :structural_hash, :call_chain_lengths, :visibility,
  keyword_init: true
)

ClassRecord = Struct.new(
  :name, :file, :type, :superclass, :methods, :source,
  :attr_reader_count, :attr_writer_count, :attr_accessor_count, :ivar_count,
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

# Exclude Helper and migration classes entirely from Feature Envy / Information Expert.
# Controllers use higher thresholds instead of full exclusion.
def excluded_entirely_from_envy_and_expert?(class_record)
  name = class_record.name.to_s
  superclass = class_record.superclass.to_s
  return true if name.end_with?("Helper")
  return true if superclass.include?("ActiveRecord::Migration")
  return true if name.match?(/\d{14}/)  # migration timestamp pattern
  false
end

def controller_class?(class_record)
  class_record.name.to_s.end_with?("Controller")
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

  # Rails/framework roots we never flag as LoD (false positive exclusions)
  LOD_ALLOWED_ROOTS = %w[
    Rails ENV I18n Time Date logger
    params request response session flash
    self
  ].freeze

  # Terminal method calls we ignore (predicates, not real LoD)
  LOD_IGNORE_TERMINALS = %w[is_a? kind_of? instance_of? class nil? present? blank?].freeze

  # Local variables: identifiers assigned in method (ident = or ident=)
  def local_vars_in_method(source)
    return [] unless source

    source.scan(/\b([a-z_][a-z0-9_]*)\s*=(?!=)/).flatten.uniq
  end

  # Method parameter names from def line
  def param_names_in_method(source)
    return [] unless source

    first_line = source.lines.first.to_s
    return [] unless first_line =~ /\bdef\s+\w+\s*\(([^)]*)\)/

    $1.scan(/\b([a-z_][a-z0-9_]*)/).flatten.uniq
  end

  # Block/iterator parameter names (do |x| or { |x, y| })
  def block_params_in_method(source)
    return [] unless source

    source.scan(/\|\s*([^|]+)\|/).flatten.flat_map { |m| m.split(/,/).map(&:strip) }.grep(/\A[a-z_][a-z0-9_]*\z/).uniq
  end

  # Extract chain details: { chain:, depth:, root:, line: }
  # Root includes @ if present. Use [^\w@] before match so we don't match
  # "assignment_form.x" as substring of "@assignment_form.x" (root would be wrong).
  def chain_details_with_lines(source)
    return [] unless source

    result = []
    source.lines.each_with_index do |line, idx|
      line_num = idx + 1
      line.scan(/(?:^|[^\w@])((\@?[a-zA-Z_][\w:]*)((?:\.[a-zA-Z_][\w]*)+))\b/) do |full, root, _rest|
        full = full.to_s.strip
        root = root.to_s.strip
        next if full.empty?
        depth = full.count(".") + 1
        next if LOD_IGNORE_TERMINALS.any? { |t| full.end_with?(".#{t}") }
        result << { chain: full, depth: depth, root: root, line: line_num }
      end
    end
    result
  end

  # True LoD: root is foreign (not owned) and depth >= LOD_FOREIGN_DEPTH + 1
  def lod_violation?(chain_info, source)
    root = chain_info[:root]
    depth = chain_info[:depth]
    return false if depth < LOD_FOREIGN_DEPTH + 1  # need depth 3 for 2 boundaries
    return false if root.start_with?("@")         # instance variable
    return false if LOD_ALLOWED_ROOTS.include?(root)
    return false if local_vars_in_method(source).include?(root)   # local variable
    return false if param_names_in_method(source).include?(root)   # method parameter
    return false if block_params_in_method(source).include?(root) # block parameter
    true
  end

  # Long chain: purely structural, depth >= LONG_CHAIN_MIN_DEPTH
  def long_chain_violation?(chain_info)
    chain_info[:depth] >= LONG_CHAIN_MIN_DEPTH
  end

  # Same exclusions for long_chain (Rails false positives)
  def chain_excluded?(chain_info)
    root = chain_info[:root]
    return true if root.start_with?("@")
    return true if LOD_ALLOWED_ROOTS.include?(root)
    return true if LOD_IGNORE_TERMINALS.any? { |t| chain_info[:chain].end_with?(".#{t}") }
    false
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

  # Unique instance variable count in class source
  def ivar_count(source)
    return 0 unless source

    source.scan(/@([\w]+)/).flatten.uniq.length
  end

  # Feature Envy: external (receiver.method on @ivar or Constant) vs own (self.method)
  def feature_envy_counts(source)
    return { external: 0, own: 0 } unless source

    external = source.scan(/@\w+\.\w+/).length +
               source.scan(/\b[A-Z]\w*(?:::\w+)*\.\w+/).length
    own = source.scan(/self\.\w+/).length
    { external: external, own: own }
  end

  # OCP: count if/elsif/case branches and type-check calls
  def ocp_branch_and_type_counts(source)
    return { branches: 0, elsif_count: 0, type_checks: 0 } unless source

    branches = source.scan(/\b(?:if|elsif|when)\b/).length + source.scan(/\bcase\b/).length
    elsif_count = source.scan(/\belsif\b/).length
    type_checks = source.scan(/\.(?:is_a\?|kind_of\?|instance_of\?)\b/).length +
                  source.scan(/\.class\s*==/).length
    { branches: branches, elsif_count: elsif_count, type_checks: type_checks }
  end

  # Words to exclude (common English, single letters, Ruby built-ins)
  SHOTGUN_EXCLUDED = %w[
    A I
    The If This That For You We There Please Not And Or But
    Returns True False Add Get Set With From When Then Also Note See Use
    String Integer Float Array Hash Symbol NilClass TrueClass FalseClass
    Numeric Object BasicObject Kernel Comparable Enumerable IO
    Doing Provide Removes Total Deleting Saved Scored
  ].freeze

  # Strip comments and string literals from source before scanning for constants
  def strip_comments_and_strings(source)
    return "" unless source

    s = source.dup

    # Remove double-quoted strings (handles \")
    s = s.gsub(/"(?:[^"\\]|\\.)*"/m, " ")
    # Remove single-quoted strings (handles \')
    s = s.gsub(/'([^'\\]|\\.)*'/m, " ")
    # Remove %q{}, %Q{}, %w[], etc.
    s = s.gsub(/%[qQwWxr]?\([^)]*\)/m, " ")
    s = s.gsub(/%[qQwWxr]?\[[^\]]*\]/m, " ")
    s = s.gsub(/%[qQwWxr]?\{[^}]*\}/m, " ")
    s = s.gsub(/%[qQwWxr]?<[^>]*>/m, " ")
    # Remove heredocs: <<IDENTIFIER ... \nIDENTIFIER
    s = s.gsub(/<<[-~]?['"]?([A-Za-z_][A-Za-z0-9_]*)['"]?\s*\n.*?^\s*\1\b/m, " ")
    # Remove line comments (whole line or inline)
    s = s.lines.map do |line|
      in_str = false
      quote = nil
      i = 0
      while i < line.length
        c = line[i]
        if !in_str && c == "#"
          line = line[0...i] + " " * (line.length - i)
          break
        end
        if (c == '"' || c == "'") && (i == 0 || line[i - 1] != "\\")
          if in_str && quote == c
            in_str = false
          elsif !in_str
            in_str = true
            quote = c
          end
        end
        i += 1
      end
      line
    end.join

    s
  end

  # Unique external class constants referenced in file source (code only, no comments/strings)
  def external_class_references(source, defined_names = [])
    return [] unless source

    code = strip_comments_and_strings(source)
    refs = []

    # ClassName.method_call (constant followed by . and lowercase method)
    code.scan(/\b([A-Z][a-zA-Z0-9_]*(?:::[A-Z][a-zA-Z0-9_]*)*)\.([a-z_][a-zA-Z0-9_]*)/) do |const, method|
      refs << const
    end

    # ClassName.new (instantiation)
    code.scan(/\b([A-Z][a-zA-Z0-9_]*(?:::[A-Z][a-zA-Z0-9_]*)*)\.new\b/) do |const|
      refs << const
    end

    # include/extend/prepend ClassName
    code.scan(/\b(?:include|extend|prepend)\s+([A-Z][a-zA-Z0-9_]*(?:::[A-Z][a-zA-Z0-9_]*)*)/) do |const|
      refs << const
    end

    # inherit < ClassName
    code.scan(/<\s*([A-Z][a-zA-Z0-9_]*(?:::[A-Z][a-zA-Z0-9_]*)*)/) do |const|
      refs << const
    end

    # rescue ClassName
    code.scan(/\brescue\s+([A-Z][a-zA-Z0-9_]*(?:::[A-Z][a-zA-Z0-9_]*)*)/) do |const|
      refs << const
    end

    # ClassName:: (namespace access)
    code.scan(/\b([A-Z][a-zA-Z0-9_]*(?:::[A-Z][a-zA-Z0-9_]*)*)::/) do |const|
      refs << const
    end

    refs = refs.uniq - defined_names

    # Exclude single letters, common words, Ruby built-ins
    refs.reject do |name|
      name.length <= 1 ||
        SHOTGUN_EXCLUDED.include?(name) ||
        name.include?("::") && SHOTGUN_EXCLUDED.include?(name.split("::").first)
    end
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
      attr_accessor_count: attr_counts[:attr_accessor],
      ivar_count:          TextMetrics.ivar_count(cls_source)
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
        TextMetrics.chain_details_with_lines(method.body).each do |info|
          next unless TextMetrics.lod_violation?(info, method.body)

          violations << {
            "file"        => cr.file,
            "class_name"  => cr.name,
            "method_name" => method.name,
            "chain"       => info[:chain],
            "depth"       => info[:depth],
            "line"        => info[:line],
            "description" => "Method `#{method.name}` in `#{cr.name}` accesses foreign object through `#{info[:chain]}` (LoD violation)."
          }
        end
      end
    end

    violations
  end

  def long_chain_violations(class_records)
    violations = []

    class_records.each do |cr|
      cr.methods.each do |method|
        TextMetrics.chain_details_with_lines(method.body).each do |info|
          next unless TextMetrics.long_chain_violation?(info)
          next if TextMetrics.chain_excluded?(info)

          violations << {
            "file"        => cr.file,
            "class_name"  => cr.name,
            "method_name" => method.name,
            "chain"       => info[:chain],
            "depth"       => info[:depth],
            "line"        => info[:line],
            "description" => "Method `#{method.name}` in `#{cr.name}` has long chain `#{info[:chain]}` (#{info[:depth]} levels)."
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

      srp_limit = cr.name.to_s.end_with?("Controller") ? SRP_CONTROLLER_MAX_METHODS : SRP_MAX_METHODS
      next if method_count < srp_limit && external_count < SRP_MAX_INITS

      description =
        if method_count >= srp_limit && external_count >= SRP_MAX_INITS
          "Class `#{cr.name}` has #{method_count} methods AND #{external_count} direct instantiations – likely doing too much."
        elsif method_count >= srp_limit
          "Class `#{cr.name}` has #{method_count} methods (limit #{srp_limit}); may violate SRP."
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
      cr.methods.each do |method|
        counts = TextMetrics.ocp_branch_and_type_counts(method.body)
        branches = counts[:branches]
        elsif_count = counts[:elsif_count]
        type_checks = counts[:type_checks]

        ocp_signal = (branches >= OCP_MIN_BRANCHES && type_checks >= OCP_TYPE_CHECK_MIN) ||
                     (method.name.match?(/\A(?:update_|handle_|process_)/) && elsif_count >= 3)

        next unless ocp_signal

        violations << {
          "file"         => cr.file,
          "class_name"   => cr.name,
          "method_name"  => method.name,
          "branch_count" => branches,
          "type_checks"  => type_checks,
          "description"  => "Method `#{method.name}` in `#{cr.name}` has #{branches} branches and #{type_checks} type checks – may violate OCP."
        }
      end
    end

    violations
  end

  def god_object_violations(class_records)
    violations = []

    class_records.each do |cr|
      next if cr.type == :module

      method_count = cr.total_methods
      ivar_count   = cr.ivar_count.to_i
      external_count = cr.instantiation_counts.keys.length

      next unless method_count >= GOD_OBJECT_MAX_METHODS ||
                  ivar_count >= GOD_OBJECT_MAX_IVARS ||
                  external_count >= GOD_OBJECT_MAX_INITS

      violations << {
        "file"           => cr.file,
        "class_name"     => cr.name,
        "method_count"   => method_count,
        "ivar_count"     => ivar_count,
        "external_count" => external_count,
        "description"    => "Class `#{cr.name}` is a God Object: #{method_count} methods, #{ivar_count} ivars, #{external_count} external instantiations."
      }
    end

    violations
  end

  def feature_envy_violations(class_records)
    violations = []

    class_records.each do |cr|
      next if excluded_entirely_from_envy_and_expert?(cr)

      min_external, ratio = controller_class?(cr) ? [FEATURE_ENVY_CONTROLLER_MIN_EXTERNAL, FEATURE_ENVY_CONTROLLER_RATIO] : [FEATURE_ENVY_MIN_EXTERNAL, FEATURE_ENVY_RATIO]

      cr.methods.each do |method|
        counts = TextMetrics.feature_envy_counts(method.body)
        external = counts[:external]
        own = counts[:own]

        next unless external >= min_external
        next unless external > (own * ratio)

        violations << {
          "file"                 => cr.file,
          "class_name"           => cr.name,
          "method_name"          => method.name,
          "external_references"  => external,
          "own_references"        => own,
          "description"          => "Method `#{method.name}` in `#{cr.name}` exhibits Feature Envy: #{external} external refs vs #{own} own."
        }
      end
    end

    violations
  end

  def long_method_violations(class_records)
    violations = []

    class_records.each do |cr|
      cr.methods.each do |method|
        line_count = method.body.lines.length
        next if line_count < LONG_METHOD_MAX_LINES

        violations << {
          "file"       => cr.file,
          "class_name" => cr.name,
          "method_name" => method.name,
          "line_count" => line_count,
          "start_line" => method.line,
          "description" => "Method `#{method.name}` in `#{cr.name}` is #{line_count} lines (max #{LONG_METHOD_MAX_LINES - 1})."
        }
      end
    end

    violations
  end

  def shotgun_surgery_violations(class_records, file_sources)
    violations = []

    file_sources.each do |file_path, source|
      defined_names = class_records
        .select { |cr| cr.file == file_path }
        .map(&:name)
      refs = TextMetrics.external_class_references(source, defined_names)
      count = refs.length
      next if count < SHOTGUN_SURGERY_MIN_EXTERNAL_CLASSES

      violations << {
        "file"                  => file_path,
        "external_class_count"  => count,
        "classes_referenced"    => refs,
        "description"           => "File references #{count} external classes – may cause shotgun surgery when modified."
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
      next if excluded_entirely_from_envy_and_expert?(cr)

      min_external, ratio = controller_class?(cr) ? [INFO_EXPERT_CONTROLLER_MIN_EXTERNAL, INFO_EXPERT_CONTROLLER_RATIO] : [INFO_EXPERT_MIN_EXTERNAL, INFO_EXPERT_RATIO]

      cr.methods.each do |method|
        dist     = TextMetrics.information_distribution(method.body)
        ivar     = dist[:ivar]
        external = dist[:external]

        next if external < min_external
        next unless external > (ivar * ratio)

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
  file_sources = {}

  file_paths.each do |path|
    unless File.exist?(path)
      parse_errors << { file: path, error: "File not found" }
      next
    end

    source = File.read(path, encoding: "utf-8", invalid: :replace, undef: :replace)
    file_sources[path] = source
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

  srp_sigs   = Detectors.srp_signals(all_records)
  ocp_viols  = Detectors.ocp_violations(all_records)
  lsp_sigs   = Detectors.lsp_signals(all_records, class_index)
  dip_viols  = Detectors.dip_violations(all_records)
  isp_viols  = Detectors.isp_violations(all_records)
  lod_viols      = Detectors.lod_violations(all_records)
  long_chain_viols = Detectors.long_chain_violations(all_records)
  dry_viols  = Detectors.dry_violations(all_records)
  ie_viols   = Detectors.information_expert_violations(all_records)
  enc_viols  = Detectors.encapsulation_violations(all_records)
  cmo_viols  = Detectors.cmo_violations(all_records)
  god_viols  = Detectors.god_object_violations(all_records)
  envy_viols = Detectors.feature_envy_violations(all_records)
  long_viols = Detectors.long_method_violations(all_records)
  shot_viols = Detectors.shotgun_surgery_violations(all_records, file_sources)

  output = {
    "srp"                   => { "signals"    => srp_sigs,   "count" => srp_sigs.length  },
    "ocp"                   => { "violations" => ocp_viols,  "count" => ocp_viols.length },
    "lsp"                   => { "signals"    => lsp_sigs,   "count" => lsp_sigs.length  },
    "dip"                   => { "violations" => dip_viols, "count" => dip_viols.length },
    "isp"                   => { "violations" => isp_viols,  "count" => isp_viols.length },
    "lod"                   => { "violations" => lod_viols,       "count" => lod_viols.length },
    "long_chain"            => { "violations" => long_chain_viols, "count" => long_chain_viols.length },
    "dry"                   => { "violations" => dry_viols, "count" => dry_viols.length },
    "information_expert"    => { "violations" => ie_viols,   "count" => ie_viols.length  },
    "encapsulation"         => { "violations" => enc_viols,  "count" => enc_viols.length },
    "cmo"                   => { "violations" => cmo_viols,  "count" => cmo_viols.length },
    "god_object"            => { "violations" => god_viols,  "count" => god_viols.length },
    "feature_envy"          => { "violations" => envy_viols, "count" => envy_viols.length },
    "long_method"           => { "violations" => long_viols, "count" => long_viols.length },
    "shotgun_surgery"       => { "violations" => shot_viols, "count" => shot_viols.length }
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
