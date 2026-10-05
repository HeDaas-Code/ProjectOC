export class ApiError extends Error { constructor(public status: number, message: string) { super(message) } }
export function csrfHeader() { return document.cookie.split('; ').find(v => v.startsWith('csrftoken='))?.split('=').slice(1).join('=') || '' }
export async function api<T = any>(path: string, method = 'GET', data?: unknown): Promise<T> {
  const headers: Record<string,string> = { 'Content-Type': 'application/json' }; if (!['GET','HEAD','OPTIONS'].includes(method)) headers['X-CSRFToken'] = decodeURIComponent(csrfHeader())
  const response = await fetch(`/api/v1/${path}`, { method, headers, credentials: 'same-origin', body: data === undefined ? undefined : JSON.stringify(data) })
  const result = response.status === 204 ? undefined : await response.json()
  if (!response.ok) throw new ApiError(response.status, typeof result.detail === 'string' ? result.detail : JSON.stringify(result))
  return result
}
export function parseEvent(block: string): { event: string; data: any } | null {
  const lines = block.replace(/\r/g, '').split('\n')
  const data = lines.filter(l => l.startsWith('data:')).map(l => l.slice(5).trim()).join('\n')
  if (!data) return null
  return { event: lines.find(l => l.startsWith('event:'))?.slice(6).trim() || 'message', data: JSON.parse(data) }
}
export async function consumeSSE(stream: ReadableStream<Uint8Array>, onEvent: (event: string, data: any) => void) {
  const reader = stream.getReader(); const decoder = new TextDecoder(); let buffer = ''; let completed = false
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
      let end: number
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const parsed = parseEvent(buffer.slice(0, end)); buffer = buffer.slice(end + 2)
        if (parsed) { onEvent(parsed.event, parsed.data); if (parsed.event === 'complete') completed = true }
      }
      if (done) break
    }
    if (!completed) throw new Error('对话流中断；输入已保留，请重新加载会话')
  } finally { reader.releaseLock() }
}
