import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const SOURCE_FILES = import.meta.glob('../**/*.tsx', {
  eager: true,
  query: '?raw',
  import: 'default',
}) as Record<string, string>
const TRANSLATABLE_ATTRIBUTES = new Set([
  'aria-label',
  'cancelText',
  'description',
  'extra',
  'label',
  'message',
  'okText',
  'placeholder',
  'title',
])
const ALLOWED_PRODUCT_TEXT = new Set([
  'PolarDB Agentic',
  'PolarDB for MySQL',
  'alibabacloud polardb tool agentic server',
  'alibabacloud polardb tool agentic server -',
  'cn-hangzhou',
  'pc-xxx',
  'v0.1.0 - Apache 2.0 License',
])

function normalizedText(value: string): string {
  return value.replace(/\s+/g, ' ').trim()
}

function containsWords(value: string): boolean {
  return /[A-Za-z]{2,}/.test(value)
}

describe('localized UI source', () => {
  it('does not introduce raw translatable JSX text or attributes', () => {
    const violations: string[] = []

    for (const [file, sourceText] of Object.entries(SOURCE_FILES)) {
      if (file.endsWith('.test.tsx')) continue
      const source = ts.createSourceFile(
        file,
        sourceText,
        ts.ScriptTarget.Latest,
        true,
        ts.ScriptKind.TSX,
      )
      const visit = (node: ts.Node) => {
        let value = ''
        if (ts.isJsxText(node)) {
          const parentTag = ts.isJsxElement(node.parent)
            ? node.parent.openingElement.tagName.getText(source)
            : ''
          if (parentTag !== 'code') value = normalizedText(node.getText(source))
        } else if (
          ts.isJsxAttribute(node) &&
          TRANSLATABLE_ATTRIBUTES.has(node.name.getText(source)) &&
          node.initializer &&
          ts.isStringLiteral(node.initializer)
        ) {
          value = normalizedText(node.initializer.text)
        }
        if (value && containsWords(value) && !ALLOWED_PRODUCT_TEXT.has(value)) {
          const position = source.getLineAndCharacterOfPosition(node.getStart(source))
          violations.push(`${file.replace(/^\.\.\//, '')}:${position.line + 1}: ${value}`)
        }
        ts.forEachChild(node, visit)
      }
      visit(source)
    }

    expect(violations).toEqual([])
  })
})
