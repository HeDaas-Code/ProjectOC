import crypto from 'node:crypto'

function decode(value) {
  return Buffer.from(value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4), 'base64')
}
function equal(a, b) {
  const left = Buffer.from(a); const right = Buffer.from(b)
  return left.length === right.length && crypto.timingSafeEqual(left, right)
}
export function verifyTicket(token, secret, now = Math.floor(Date.now() / 1000)) {
  if (typeof token !== 'string') throw new Error('missing ticket')
  const [version, encoded, signature] = token.split('.')
  if (version !== 'v1' || !encoded || !signature) throw new Error('invalid ticket')
  const expected = crypto.createHmac('sha256', secret).update(encoded).digest('base64url')
  if (!equal(signature, expected)) throw new Error('invalid ticket signature')
  let payload
  try { payload = JSON.parse(decode(encoded).toString('utf8')) } catch { throw new Error('invalid ticket payload') }
  if (payload.v !== 1 || !payload.sub || !payload.workspace || !payload.canvas || !payload.role || !payload.client_id || payload.exp <= now) throw new Error('expired or invalid ticket')
  // Room connection additionally requires an explicit signed branch claim.
  if (payload.branch !== undefined && (typeof payload.branch !== 'string' || !payload.branch)) throw new Error('invalid branch claim')
  if (!['owner', 'editor', 'reader'].includes(payload.role)) throw new Error('invalid role')
  return payload
}
