export function parseAllowedOrigins(value = '') {
  return new Set(value.split(',').map(item => item.trim()).filter(Boolean))
}

export function originAllowed(req, allowedOrigins, requireOrigin = false) {
  const origin = req.headers.origin
  if (!origin) return !requireOrigin
  if (allowedOrigins.size === 0) return !requireOrigin
  return allowedOrigins.has(origin)
}

export function acceptMessage(client, now, windowMs, maxMessages) {
  client.messageTimes = client.messageTimes.filter(timestamp => now - timestamp < windowMs)
  if (client.messageTimes.length >= maxMessages) return false
  client.messageTimes.push(now)
  return true
}

export function sanitizePresence(state) {
  if (!state || typeof state !== 'object') return {}
  const result = {}
  if (state.cursor === null) result.cursor = null
  else if (state.cursor && Number.isFinite(state.cursor.x) && Number.isFinite(state.cursor.y)) {
    result.cursor = {
      x: Math.max(-100_000, Math.min(100_000, state.cursor.x)),
      y: Math.max(-100_000, Math.min(100_000, state.cursor.y)),
    }
  }
  if (Array.isArray(state.selection)) {
    result.selection = state.selection.filter(value => typeof value === 'string').slice(0, 500)
  }
  return result
}
