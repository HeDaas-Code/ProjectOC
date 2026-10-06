import crypto from 'node:crypto'
import http from 'node:http'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import { WebSocketServer } from 'ws'
import { verifyTicket } from './ticket.js'
import { originAllowed, parseAllowedOrigins } from './policy.js'
import { applyOperations, createRecordState, snapshotFromRecordState } from './legacy-record-migration.js'
import { createRoomLease } from './room-lease.js'
import { OfficialRoomManager, OFFICIAL_PROTOCOL, OFFICIAL_SCHEMA_VERSION } from './tldraw-room.js'

const DEFAULTS = Object.freeze({
  port: 8787,
  djangoUrl: 'http://backend:8000',
  internalSecret: 'projectoc-sync-local-secret',
  ticketSecret: 'projectoc-development-secret-key',
  allowedOrigins: '',
  requireOrigin: false,
  maxRoomClients: 50,
  maxSnapshotBytes: 6 * 1024 * 1024,
  redisUrl: '',
  redisPrefix: 'projectoc:sync',
  instanceId: '',
  officialSchemaVersion: OFFICIAL_SCHEMA_VERSION,
  roomLeaseTtlMs: 30_000,
})

const json = (res, status, value) => {
  res.writeHead(status, {'content-type': 'application/json'})
  res.end(JSON.stringify(value))
}
const rejectUpgrade = (socket, status, detail) => {
  socket.write(`HTTP/1.1 ${status} Unauthorized\r\nConnection: close\r\nContent-Type: text/plain\r\n\r\n${detail}`)
  socket.destroy()
}

/**
 * Create an isolated sync server.  Keeping construction side-effect free makes
 * the room protocol testable without starting a process or requiring Django.
 */
export function createSyncServer(options = {}) {
  const config = {
    ...DEFAULTS,
    port: Number(process.env.PORT || DEFAULTS.port),
    djangoUrl: (process.env.DJANGO_INTERNAL_URL || DEFAULTS.djangoUrl).replace(/\/$/, ''),
    internalSecret: process.env.SYNC_INTERNAL_SECRET || DEFAULTS.internalSecret,
    ticketSecret: process.env.SYNC_TICKET_SECRET || process.env.DJANGO_SECRET_KEY || DEFAULTS.ticketSecret,
    allowedOrigins: process.env.SYNC_ALLOWED_ORIGINS || DEFAULTS.allowedOrigins,
    requireOrigin: process.env.SYNC_REQUIRE_ORIGIN === '1',
    maxRoomClients: Math.max(1, Number(process.env.SYNC_MAX_ROOM_CLIENTS || DEFAULTS.maxRoomClients)),
    maxSnapshotBytes: Math.max(64 * 1024, Number(process.env.SYNC_MAX_SNAPSHOT_BYTES || DEFAULTS.maxSnapshotBytes)),
    redisUrl: process.env.SYNC_REDIS_URL || process.env.REDIS_URL || DEFAULTS.redisUrl,
    redisPrefix: process.env.SYNC_REDIS_PREFIX || DEFAULTS.redisPrefix,
    instanceId: process.env.SYNC_INSTANCE_ID || crypto.randomUUID(),
    officialSchemaVersion: process.env.TLDRAW_SCHEMA_VERSION || DEFAULTS.officialSchemaVersion,
    roomLeaseTtlMs: Math.max(5_000, Number(process.env.SYNC_ROOM_LEASE_TTL_MS || DEFAULTS.roomLeaseTtlMs)),
    officialDataDir: process.env.TLDRAW_SYNC_DATA_DIR || undefined,
    ...options,
  }
  const fetchImpl = options.fetchImpl || fetch
  const allowedOrigins = parseAllowedOrigins(config.allowedOrigins)
  const instanceId = config.instanceId
  const roomLease = options.roomLease ?? createRoomLease({ url: config.redisUrl, prefix: config.redisPrefix, instanceId, ttlMs: config.roomLeaseTtlMs })
  const officialRooms = options.officialRooms ?? new OfficialRoomManager({
    dataDir: config.officialDataDir,
    loadLegacySnapshot: async roomKey => {
      const canvas = String(roomKey).split(':').at(-1)
      const state = await loadState(canvas)
      if (state.sync_metadata?.protocol !== OFFICIAL_PROTOCOL) {
        const recordState = createRecordState(state.snapshot || {}, state.sync_metadata || {})
        let cursor = Number(state.snapshot_operation_cursor || 0)
        const target = Number(state.operation_cursor || 0)
        if (Number(state.operation_compacted_through || 0) > cursor) throw new Error('legacy migration log has a gap; restore a backup')
        while (cursor < target) {
          const page = await loadOperations(canvas, cursor, 1000)
          const entries = page.operations || []
          if (!entries.length || entries[0].sequence !== cursor + 1) throw new Error('legacy migration log is incomplete')
          for (const entry of entries) {
            if (entry.sequence !== cursor + 1) throw new Error('legacy migration sequence gap')
            const result = applyOperations(recordState, [entry.operation])
            if (result.deferred.length || result.rejected.some(item => !['duplicate', 'stale_operation'].includes(item.reason))) throw new Error('legacy migration operation cannot be recovered')
            cursor = entry.sequence
          }
        }
        if (target > Number(state.snapshot_operation_cursor || 0)) {
          state.snapshot = snapshotFromRecordState(state.snapshot, recordState)
          state.snapshot_hash = ''
        }
      }
      officialVersions.set(roomKey, Number(state.snapshot_version) || 1)
      return {
        snapshot: state.snapshot || {},
        snapshot_hash: state.snapshot_hash || '',
        sync_metadata: state.sync_metadata || {},
        latest_sync_event: state.latest_sync_event || null,
      }
    },
    loadDurableEvents: async (roomKey, after = 0, targetClock = Number.MAX_SAFE_INTEGER) => {
      const canvas = String(roomKey).split(':').at(-1)
      let cursor = Math.max(0, Number(after) || 0)
      const events = []
      for (;;) {
        const body = await loadSyncEvents(canvas, cursor, 1000)
        const page = Array.isArray(body.events) ? body.events : []
        events.push(...page.filter(event => Number(event.document_clock) <= Number(targetClock)))
        const last = page.at(-1)
        const nextCursor = Number(body.cursor ?? last?.document_clock ?? cursor)
        if (!body.has_more || !page.length || !Number.isSafeInteger(nextCursor) || nextCursor <= cursor) break
        cursor = nextCursor
      }
      return events
    },
    persistReconciliation: async (roomKey, reconciliation) => {
      const canvas = String(roomKey).split(':').at(-1)
      const response = await fetchImpl(`${config.djangoUrl}/api/v1/canvases/${canvas}/sync-reconciliation/`, {
        method: 'POST',
        headers: {'content-type': 'application/json', 'X-Sync-Internal-Secret': config.internalSecret},
        body: JSON.stringify({reconciliation}),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(body.detail || `reconciliation persistence failed (${response.status})`)
      return body
    },
    persistSnapshot: async (roomKey, snapshot, metadata) => {
      const canvas = String(roomKey).split(':').at(-1)
      const expectedVersion = officialVersions.get(roomKey) || 1
      const response = await fetchImpl(`${config.djangoUrl}/api/v1/canvases/${canvas}/sync-state/`, {
        method: 'POST',
        headers: {'content-type': 'application/json', 'X-Sync-Internal-Secret': config.internalSecret},
        body: JSON.stringify({snapshot, sync_metadata: metadata, expected_version: expectedVersion}),
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(body.detail || `official snapshot persistence failed (${response.status})`)
      officialVersions.set(roomKey, Number(body.snapshot_version) || expectedVersion + 1)
      return body
    },
  })
  const officialVersions = new Map()
  const officialLeases = new Map()
  const leaseRenewers = new Map()
  const acquiringRooms = new Map()

  function officialRoomKey(claims, branch) {
    return `${claims.workspace}:${branch}:${claims.canvas}`
  }

  async function acquireOfficialRoom(claims, branch) {
    const key = officialRoomKey(claims, branch)
    if (acquiringRooms.has(key)) return acquiringRooms.get(key)
    const acquiring = initializeOfficialRoom(claims, branch)
    acquiringRooms.set(key, acquiring)
    try { return await acquiring } finally { acquiringRooms.delete(key) }
  }

  async function initializeOfficialRoom(claims, branch) {
    const roomKey = officialRoomKey(claims, branch)
    const existing = officialLeases.get(roomKey)
    if (existing) return {roomKey, state: await officialRooms.getRoom(roomKey)}
    const lease = await roomLease.acquire(roomKey)
    if (!lease.acquired) throw new Error(`room lease held by ${lease.owner || 'another instance'}`)
    officialLeases.set(roomKey, lease)
    const renewer = setInterval(async () => {
      try {
        if (!await roomLease.renew(roomKey)) {
          const state = officialRooms.rooms.get(roomKey)
          if (state) officialRooms.closeRoom(roomKey, {code: 1012, reason: 'room_lease_lost;reconnect'})
          clearInterval(leaseRenewers.get(roomKey))
          leaseRenewers.delete(roomKey)
          officialLeases.delete(roomKey)
        } else {
          const state = officialRooms.rooms.get(roomKey)
          if (state && !officialRooms.loadingRooms?.has(roomKey) && state.room.getNumActiveSessions() === 0) await releaseOfficialRoom(roomKey)
        }
      } catch {
        const state = officialRooms.rooms.get(roomKey)
        if (state) officialRooms.closeRoom(roomKey, {code: 1012, reason: 'room_lease_unavailable;reconnect'})
      }
    }, Math.max(1000, Math.floor(config.roomLeaseTtlMs / 3)))
    renewer.unref?.()
    leaseRenewers.set(roomKey, renewer)
    try {
      return {roomKey, state: await officialRooms.getRoom(roomKey)}
    } catch (error) {
      clearInterval(renewer)
      leaseRenewers.delete(roomKey)
      officialLeases.delete(roomKey)
      await roomLease.release(roomKey).catch(() => {})
      throw error
    }
  }

  async function releaseOfficialRoom(roomKey, force = false) {
    const state = officialRooms.rooms.get(roomKey)
    // Keep retrying a disconnected room until PostgreSQL catches up.
    if (!force && state && (state.persistPending?.length || state.persistInFlight)) return
    const timer = leaseRenewers.get(roomKey)
    if (timer) clearInterval(timer)
    leaseRenewers.delete(roomKey)
    if (officialRooms.rooms.has(roomKey)) officialRooms.closeRoom(roomKey)
    officialLeases.delete(roomKey)
    await roomLease.release(roomKey).catch(() => {})
  }

  async function loadState(canvas) {
    const response = await fetchImpl(`${config.djangoUrl}/api/v1/canvases/${canvas}/sync-state/`, { headers: {'X-Sync-Internal-Secret': config.internalSecret} })
    if (!response.ok) throw new Error(`Django state request failed (${response.status})`)
    return response.json()
  }

  async function loadSyncEvents(canvas, after, limit = 200) {
    const response = await fetchImpl(`${config.djangoUrl}/api/v1/canvases/${canvas}/sync-events/?after=${encodeURIComponent(after)}&limit=${encodeURIComponent(limit)}`, {
      method: 'GET',
      headers: {'X-Sync-Internal-Secret': config.internalSecret},
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.detail || `sync event replay failed (${response.status})`)
    return body
  }

  async function loadOperations(canvas, after, limit = 200) {
    const response = await fetchImpl(`${config.djangoUrl}/api/v1/canvases/${canvas}/sync-ops/?after=${encodeURIComponent(after)}&limit=${encodeURIComponent(limit)}`, {
      method: 'GET',
      headers: {'X-Sync-Internal-Secret': config.internalSecret},
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.detail || `operation replay failed (${response.status})`)
    return body
  }

  const server = http.createServer((req, res) => {
    if (req.url === '/health' || req.url === '/health/') return json(res, 200, {ok: true, official_rooms: officialRooms.health(), reconciliation: officialRooms.healthSummary(), room_leases: {active: officialLeases.size, health: roomLease.health}, max_room_clients: config.maxRoomClients, instance_id: instanceId, protocol: OFFICIAL_PROTOCOL, schema_version: config.officialSchemaVersion, official_crdt: {enabled: true, protocol: OFFICIAL_PROTOCOL, schema_version: config.officialSchemaVersion}, capabilities: [OFFICIAL_PROTOCOL]})
    return json(res, 404, {detail: 'not found'})
  })
  const wss = new WebSocketServer({ noServer: true, maxPayload: config.maxSnapshotBytes + 1024 * 1024 })
  server.on('upgrade', async (req, socket, head) => {
    try {
      if (!originAllowed(req, allowedOrigins, config.requireOrigin)) return rejectUpgrade(socket, 403, 'Forbidden origin')
      const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`)
      const match = url.pathname.match(/^\/rooms\/([^/]+)$/)
      const claims = verifyTicket(url.searchParams.get('ticket'), config.ticketSecret)
      const requestedProtocol = url.searchParams.get('protocol') || OFFICIAL_PROTOCOL
      const requestedSchema = url.searchParams.get('schema') || config.officialSchemaVersion
      if (requestedProtocol !== OFFICIAL_PROTOCOL) return rejectUpgrade(socket, 410, 'records-v1 retired; refresh the client to use tldraw-sync-v2')
      if (!match || claims.canvas !== match[1]) throw new Error('ticket room mismatch')
      if (requestedProtocol === OFFICIAL_PROTOCOL) {
        if (requestedSchema !== config.officialSchemaVersion) return rejectUpgrade(socket, 409, 'TLDraw schema upgrade required')
        const branch = url.searchParams.get('branch') || claims.branch
        if (!branch || !claims.branch || claims.branch !== branch) return rejectUpgrade(socket, 403, 'Branch claim required')
        const {roomKey, state} = await acquireOfficialRoom(claims, branch)
        if (state.room.getNumActiveSessions() >= config.maxRoomClients) return rejectUpgrade(socket, 429, 'Room is full')
        wss.handleUpgrade(req, socket, head, ws => wss.emit('official-connection', ws, req, state, roomKey, claims, requestedSchema))
        return
      }
    } catch (error) {
      rejectUpgrade(socket, 401, error?.message || 'Unauthorized')
    }
  })

  wss.on('official-connection', (ws, _req, roomState, roomKey, claims, requestedSchema) => {
    const sessionId = `${claims.client_id}:${crypto.randomUUID()}`
    try {
      officialRooms.connect(roomState, {sessionId, ws, isReadonly: claims.role === 'reader', meta: { ...claims, roomKey }})
    } catch (error) {
      ws.close(1011, 'Unable to join room')
      return
    }
    const release = async () => {
      if (ws.__projectocOfficialReleased) return
      ws.__projectocOfficialReleased = true
      // TLSocketRoom owns its session lifecycle. Once the last transport has
      // gone away, release the lease and close the in-process room so another
      // instance can safely become the owner.
      if (roomState.room.getNumActiveSessions() === 0) await releaseOfficialRoom(roomKey)
    }
    ws.once('close', () => { void release() })
    ws.once('error', () => { void release() })
  })

  async function close() {
    for (const client of wss.clients) client.close()
    for (const roomKey of [...officialLeases.keys()]) await releaseOfficialRoom(roomKey, true)
    officialRooms.close()
    await roomLease.close().catch(() => {})
    await new Promise(resolve => wss.close(() => resolve()))
    await new Promise(resolve => server.close(() => resolve()))
  }

  return {server, wss, officialRooms, config, listen: (...args) => server.listen(...args), close}
}

export function startSyncServer() {
  const app = createSyncServer()
  app.listen(app.config.port, '0.0.0.0', () => console.log(`ProjectOC sync service listening on ${app.config.port}`))
  return app
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) startSyncServer()
