import DOMPurify from 'dompurify'
import { marked } from 'marked'

marked.setOptions({ breaks: true, gfm: true })

function fallbackSanitize(html: string): string {
  // Vitest's Node environment has no DOM. Production always uses DOMPurify;
  // this conservative fallback keeps server-side rendering/tests safe too.
  return html
    .replace(/<\/?(script|style|iframe|object|embed)[^>]*>/gi, '')
    .replace(/\s+on[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '')
    .replace(/(href|src)\s*=\s*(["'])\s*javascript:[^"']*\2/gi, '$1="#"')
    .replace(/(href|src)\s*=\s*javascript:[^\s>]+/gi, '$1="#"')
    .replace(/javascript:/gi, '')
}

/** Render chat Markdown as safe HTML. Raw HTML is sanitized before v-html. */
export function renderMarkdown(content: string): string {
  const parsed = String(marked.parse(content || '', { async: false }))
  if (typeof DOMPurify.sanitize === 'function') {
    return DOMPurify.sanitize(parsed, { USE_PROFILES: { html: true } })
  }
  return fallbackSanitize(parsed)
}
