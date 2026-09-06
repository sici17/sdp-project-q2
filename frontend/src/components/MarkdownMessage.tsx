import { Fragment, type ReactNode } from 'react'
import Box from '@mui/material/Box'

interface MarkdownMessageProps {
  content: string
}

// The escape alternative comes first so a backslashed character is consumed
// before it can be read as emphasis. Models escape underscores when writing
// markdown, so an alarm code reached the operator with its backslashes shown.
const inlineTokenPattern = /(\\[\\`*_{}[\]()#+\-.!~|>]|`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*)/g
const headingPattern = /^(#{1,6})\s+(.+)$/
const unorderedItemPattern = /^[-*+]\s+(.+)$/
const orderedItemPattern = /^\d+[.)]\s+(.+)$/

function inlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = []
  let cursor = 0
  let tokenIndex = 0

  for (const match of text.matchAll(inlineTokenPattern)) {
    const start = match.index ?? 0
    const token = match[0]
    if (start > cursor) {
      nodes.push(text.slice(cursor, start))
    }

    const key = `${keyPrefix}-${tokenIndex}`
    if (token.startsWith('\\')) {
      // The escaped character itself, without the backslash that protected it.
      nodes.push(token.slice(1))
    } else if (token.startsWith('**')) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>)
    } else if (token.startsWith('`')) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>)
    } else {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>)
    }
    tokenIndex += 1
    cursor = start + token.length
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor))
  }
  return nodes
}

function startsBlock(line: string) {
  return (
    headingPattern.test(line) ||
    unorderedItemPattern.test(line) ||
    orderedItemPattern.test(line) ||
    line.startsWith('```')
  )
}

/**
 * Render the small Markdown subset used by model answers without accepting raw HTML.
 * React escapes every text node, so a model cannot inject script or arbitrary markup.
 */
export function MarkdownMessage({ content }: MarkdownMessageProps) {
  const lines = content.replaceAll('\r\n', '\n').split('\n')
  const blocks: ReactNode[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) {
      index += 1
      continue
    }

    const heading = line.match(headingPattern)
    if (heading) {
      const Heading = heading[1].length <= 2 ? 'h3' : 'h4'
      blocks.push(
        <Heading key={`heading-${index}`}>
          {inlineMarkdown(heading[2], `heading-${index}`)}
        </Heading>,
      )
      index += 1
      continue
    }

    if (line.startsWith('```')) {
      const codeLines: string[] = []
      index += 1
      while (index < lines.length && !lines[index].startsWith('```')) {
        codeLines.push(lines[index])
        index += 1
      }
      index += index < lines.length ? 1 : 0
      blocks.push(
        <pre key={`code-${index}`}>
          <code>{codeLines.join('\n')}</code>
        </pre>,
      )
      continue
    }

    const unordered = line.match(unorderedItemPattern)
    if (unordered) {
      const items: string[] = []
      while (index < lines.length) {
        const item = lines[index].match(unorderedItemPattern)
        if (!item) break
        items.push(item[1])
        index += 1
      }
      blocks.push(
        <ul key={`unordered-${index}`}>
          {items.map((item, itemIndex) => (
            <li key={`unordered-${index}-${itemIndex}`}>
              {inlineMarkdown(item, `unordered-${index}-${itemIndex}`)}
            </li>
          ))}
        </ul>,
      )
      continue
    }

    const ordered = line.match(orderedItemPattern)
    if (ordered) {
      const items: string[] = []
      while (index < lines.length) {
        const item = lines[index].match(orderedItemPattern)
        if (!item) break
        items.push(item[1])
        index += 1
      }
      blocks.push(
        <ol key={`ordered-${index}`}>
          {items.map((item, itemIndex) => (
            <li key={`ordered-${index}-${itemIndex}`}>
              {inlineMarkdown(item, `ordered-${index}-${itemIndex}`)}
            </li>
          ))}
        </ol>,
      )
      continue
    }

    const paragraphLines: string[] = []
    while (index < lines.length && lines[index].trim() && !startsBlock(lines[index])) {
      paragraphLines.push(lines[index])
      index += 1
    }
    blocks.push(
      <p key={`paragraph-${index}`}>
        {paragraphLines.map((paragraphLine, lineIndex) => (
          <Fragment key={`paragraph-${index}-${lineIndex}`}>
            {inlineMarkdown(paragraphLine, `paragraph-${index}-${lineIndex}`)}
            {lineIndex < paragraphLines.length - 1 ? <br /> : null}
          </Fragment>
        ))}
      </p>,
    )
  }

  return (
    <Box
      className="message-content"
      sx={{
        // An alarm code is one unbroken 28-character token. On a phone it ran
        // past the bubble and put a horizontal scrollbar under the whole
        // conversation, so long identifiers break rather than overflow.
        minWidth: 0,
        overflowWrap: 'anywhere',
        '& p': { my: 0.5 },
        '& ul, & ol': { pl: 2.5, my: 0.5 },
        '& h1, & h2, & h3, & h4': { fontSize: '1rem', fontWeight: 600, mt: 1, mb: 0.5 },
        '& pre': { overflowX: 'auto', maxWidth: '100%' },
        '& code': {
          px: 0.5,
          borderRadius: 0.5,
          bgcolor: 'action.hover',
          fontFamily: 'monospace',
          fontSize: '0.875em',
        },
      }}
    >
      {blocks}
    </Box>
  )
}
