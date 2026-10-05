import crypto from 'node:crypto'
import { createClient } from 'redis'

/**
 * A small pub/sub boundary so the room engine can be tested without Redis.
 * Messages are intentionally ephemeral: PostgreSQL's operation log remains the
 * durable source and is used for catch-up after a missed publication.
 */
export function createRedisPubSub({ url, prefix = 'projectoc:sync', instanceId = crypto.randomUUID() } = {}) {
  if (!url) return null
  const publisher = createClient({ url })
  const subscriber = createClient({ url })
  let connected = false
  let connecting
  const handlers = new Map()
  const health = { mode: 'redis', status: 'disconnected', lastError: undefined }

  publisher.on('error', error => { health.status = 'error'; health.lastError = String(error) })
  subscriber.on('error', error => { health.status = 'error'; health.lastError = String(error) })

  async function connect() {
    if (connected) return
    if (!connecting) {
      connecting = Promise.all([publisher.connect(), subscriber.connect()]).then(() => {
        connected = true
        health.status = 'ready'
        health.lastError = undefined
      }).catch(error => {
        connecting = undefined
        health.status = 'error'
        health.lastError = String(error)
        throw error
      })
    }
    await connecting
  }

  async function subscribe(channel, handler) {
    await connect()
    let listeners = handlers.get(channel)
    if (!listeners) {
      listeners = new Set()
      handlers.set(channel, listeners)
      await subscriber.subscribe(channel, raw => {
        let value
        try { value = JSON.parse(raw) } catch { return }
        for (const listener of listeners) void listener(value)
      })
    }
    listeners.add(handler)
    return async () => {
      listeners.delete(handler)
      if (!listeners.size) {
        handlers.delete(channel)
        await subscriber.unsubscribe(channel)
      }
    }
  }

  return {
    instanceId,
    health,
    async publish(channel, value) {
      await connect()
      await publisher.publish(channel, JSON.stringify(value))
    },
    subscribe,
    async close() {
      for (const channel of handlers.keys()) await subscriber.unsubscribe(channel).catch(() => {})
      handlers.clear()
      if (subscriber.isOpen) await subscriber.quit().catch(() => {})
      if (publisher.isOpen) await publisher.quit().catch(() => {})
      connected = false
      connecting = undefined
      health.status = 'closed'
    },
  }
}

/** Deterministic in-memory transport used by multi-instance protocol tests. */
export function createInMemoryPubSubHub() {
  const channels = new Map()
  const createClient = (instanceId = crypto.randomUUID()) => ({
    instanceId,
    health: { mode: 'memory', status: 'ready' },
    async publish(channel, value) {
      for (const handler of channels.get(channel) || []) queueMicrotask(() => handler(structuredClone(value)))
    },
    async subscribe(channel, handler) {
      const listeners = channels.get(channel) || new Set()
      listeners.add(handler)
      channels.set(channel, listeners)
      return async () => {
        listeners.delete(handler)
        if (!listeners.size) channels.delete(channel)
      }
    },
    async close() {},
  })
  return { createClient }
}
