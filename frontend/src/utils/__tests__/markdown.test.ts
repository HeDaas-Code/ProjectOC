import { describe, expect, it } from 'vitest'
import { renderMarkdown } from '../markdown'

describe('renderMarkdown', () => {
  it('renders common Markdown structures', () => {
    const html = renderMarkdown('# 标题\n\n**重点**\n\n- 条目\n\n```py\nprint(1)\n```')
    expect(html).toContain('<h1>标题</h1>')
    expect(html).toContain('<strong>重点</strong>')
    expect(html).toContain('<li>条目</li>')
    expect(html).toContain('<pre><code class="language-py">print(1)')
  })

  it('sanitizes raw HTML and unsafe links', () => {
    const html = renderMarkdown('<script>alert(1)</script><img src=x onerror="alert(1)"> [坏链接](javascript:alert(1))')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('onerror')
    expect(html).not.toContain('javascript:')
  })
})
