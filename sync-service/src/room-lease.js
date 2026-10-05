import crypto from 'node:crypto'
import { createClient } from 'redis'

// A room lease is deliberately separate from the pub/sub connection. Pub/sub
// is best-effort broadcast; the lease is the safety boundary that prevents two
// TLSocketRoom instances from owning the same document at once.
export function createRoomLease({ url = '', prefix = 'projectoc:sync', instanceId = crypto.randomUUID(), ttlMs = 30_000, redisClient } = {}) {
  const keyFor = roomKey => `${prefix}:room-lease:${roomKey}`
  const client = redisClient || (url ? createClient({ url }) : null)
  const local = new Map()
  let connecting
  const health = { mode: client ? 'redis' : 'process', status: client ? 'disconnected' : 'ready', instance_id: instanceId }

  async function connect() {
    if (!client) return
    if (client.isOpen) { health.status = 'ready'; return }
    if (!connecting) {
      connecting = client.connect().then(() => { health.status = 'ready' }).catch(error => {
        health.status = 'error'; health.error = String(error); throw error
      }).finally(() => { connecting = undefined })
    }
    await connecting
  }

  async function acquire(roomKey) {
    const key = keyFor(roomKey)
    if (!client) {
      const current = local.get(key)
      if (current && current.owner !== instanceId && current.expiresAt > Date.now()) return { acquired: false, owner: current.owner }
      local.set(key, { owner: instanceId, expiresAt: Date.now() + ttlMs })
      return { acquired: true, owner: instanceId, mode: 'process' }
    }
    await connect()
    const acquired = await client.set(key, instanceId, { NX: true, PX: ttlMs })
    if (acquired === 'OK') return { acquired: true, owner: instanceId, mode: 'redis' }
    const owner = await client.get(key).catch(() => undefined)
    return { acquired: false, owner: owner || 'unknown', mode: 'redis' }
  }

  async function renew(roomKey) {
    const key = keyFor(roomKey)
    if (!client) {
      const current = local.get(key)
      if (!current || current.owner !== instanceId) return false
      current.expiresAt = Date.now() + ttlMs
      return true
    }
    await connect()
    const result = await client.eval(
      'if redis.call("get", KEYS[1]) == ARGV[1] then return redis.call("pexpire", KEYS[1], ARGV[2]) else return 0 end',
      { keys: [key], arguments: [instanceId, String(ttlMs)] },
    )
    return Number(result) === 1
  }

  async function release(roomKey) {
    const key = keyFor(roomKey)
    if (!client) {
      const current = local.get(key)
      if (current?.owner === instanceId) local.delete(key)
      return true
    }
    await connect()
    const result = await client.eval(
      'if redis.call("get", KEYS[1]) == ARGV[1] then return redis.call("del", KEYS[1]) else return 0 end',
      { keys: [key], arguments: [instanceId] },
    )
    return Number(result) === 1
  }

  return {
    health,
    acquire,
    renew,
    release,
    async close() {
      if (client?.isOpen) await client.quit().catch(() => {})
      health.status = 'closed'
    },
  }
}
