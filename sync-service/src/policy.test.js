import test from 'node:test'
import assert from 'node:assert/strict'
import { acceptMessage, originAllowed, parseAllowedOrigins, sanitizePresence } from './policy.js'

test('origin policy allows configured origins and rejects cross-site origins', () => {
  const allowed = parseAllowedOrigins('https://studio.example, https://localhost:5173')
  assert.equal(originAllowed({ headers: { origin: 'https://studio.example' } }, allowed, true), true)
  assert.equal(originAllowed({ headers: { origin: 'https://evil.example' } }, allowed, true), false)
  assert.equal(originAllowed({ headers: {} }, allowed, true), false)
  assert.equal(originAllowed({ headers: {} }, allowed, false), true)
  assert.equal(originAllowed({ headers: { origin: 'https://anything.example' } }, new Set(), true), false)
  assert.equal(originAllowed({ headers: { origin: 'https://anything.example' } }, new Set(), false), true)
})

test('message rate limiter uses a sliding window', () => {
  const client = { messageTimes: [] }
  assert.equal(acceptMessage(client, 1000, 1000, 2), true)
  assert.equal(acceptMessage(client, 1100, 1000, 2), true)
  assert.equal(acceptMessage(client, 1200, 1000, 2), false)
  assert.equal(acceptMessage(client, 2101, 1000, 2), true)
})

test('presence is constrained to the supported wire shape', () => {
  const state = sanitizePresence({
    cursor: { x: 999999, y: -999999 },
    selection: ['a', 1, 'b', ...Array.from({ length: 600 }, (_, i) => String(i))],
    admin: 'must not cross the wire',
  })
  assert.deepEqual(state.cursor, { x: 100000, y: -100000 })
  assert.equal(state.selection.length, 500)
  assert.equal(state.admin, undefined)
  assert.deepEqual(sanitizePresence({ cursor: null }), { cursor: null })
})
