#!/usr/bin/env node
/**
 * Parses a TypeScript/TSX file and outputs JSON with components, hooks, and imports.
 * Usage: npx ts-node ts_parser.ts <file_path>  (or: npx tsx ts_parser.ts <file_path>)
 */

import * as fs from "fs";
import * as path from "path";
import * as ts from "typescript";

interface ParseResult {
  components: string[];
  hooks: string[];
  imports: { module: string; specifiers: string[] }[];
}

function parseFile(filePath: string): ParseResult {
  const content = fs.readFileSync(filePath, "utf-8");
  const ext = path.extname(filePath);
  const isJsx = /\.(tsx|jsx)$/i.test(ext);

  const sourceFile = ts.createSourceFile(
    filePath,
    content,
    ts.ScriptTarget.Latest,
    true,
    isJsx ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  );

  const result: ParseResult = {
    components: [],
    hooks: [],
    imports: [],
  };

  const hookNames = new Set<string>();
  const componentNames = new Set<string>();
  const importMap = new Map<string, Set<string>>();

  function visit(node: ts.Node) {
    // ImportDeclaration: import { X } from "module" or import X from "module"
    if (ts.isImportDeclaration(node)) {
      const moduleSpecifier = node.moduleSpecifier;
      if (ts.isStringLiteral(moduleSpecifier)) {
        const module = moduleSpecifier.text;
        const specifiers: string[] = [];

        if (node.importClause) {
          if (node.importClause.name) {
            specifiers.push(node.importClause.name.text);
          }
          if (node.importClause.namedBindings) {
            if (ts.isNamespaceImport(node.importClause.namedBindings)) {
              specifiers.push(node.importClause.namedBindings.name.text + " (namespace)");
            } else if (ts.isNamedImports(node.importClause.namedBindings)) {
              for (const spec of node.importClause.namedBindings.elements) {
                specifiers.push(spec.name.text);
              }
            }
          }
        }

        if (!importMap.has(module)) {
          importMap.set(module, new Set());
        }
        specifiers.forEach((s) => importMap.get(module)!.add(s));
      }
    }

    // CallExpression: look for identifier calls (hooks)
    if (ts.isCallExpression(node)) {
      const expr = node.expression;
      if (ts.isIdentifier(expr) && expr.text.startsWith("use")) {
        hookNames.add(expr.text);
      }
    }

    // VariableDeclaration: const Foo = () => {} or const Foo = function() {}
    if (ts.isVariableStatement(node)) {
      for (const decl of node.declarationList.declarations) {
        if (ts.isIdentifier(decl.name) && decl.initializer) {
          const name = decl.name.text;
          if (name[0] === name[0].toUpperCase()) {
            const init = decl.initializer;
            if (
              ts.isArrowFunction(init) ||
              ts.isFunctionExpression(init) ||
              ts.isCallExpression(init) // React.forwardRef, etc.
            ) {
              componentNames.add(name);
            }
          }
        }
      }
    }

    // FunctionDeclaration: function Foo() {}
    if (ts.isFunctionDeclaration(node) && node.name) {
      const name = node.name.text;
      if (name[0] === name[0].toUpperCase()) {
        componentNames.add(name);
      }
    }

    ts.forEachChild(node, visit);
  }

  visit(sourceFile);

  result.components = Array.from(componentNames).sort();
  result.hooks = Array.from(hookNames).sort();
  result.imports = Array.from(importMap.entries()).map(([module, specifiers]) => ({
    module,
    specifiers: Array.from(specifiers).sort(),
  }));

  return result;
}

// CLI entry when this file is executed directly (not when imported)
const isMain = process.argv[1]?.includes("ts_parser");
if (isMain) {
  const filePath = process.argv[2];
  if (!filePath) {
    console.error("Usage: npx tsx ts_parser.ts <file_path>");
    process.exit(1);
  }
  if (!fs.existsSync(filePath)) {
    console.error(`File not found: ${filePath}`);
    process.exit(1);
  }
  const output = parseFile(filePath);
  console.log(JSON.stringify(output, null, 2));
}

export { parseFile, ParseResult };
