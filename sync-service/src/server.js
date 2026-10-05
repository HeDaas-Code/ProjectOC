import crypto from 'node:crypto'
import http from 'node:http'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import { WebSocketServer } from 'ws'
import { verifyTicket } from './ticket.js'
import { acceptMessage, originAllowed, parseAllowedOrigins, sanitizePresence } from './policy.js'
import { applyOperations, createRecordState, metadataFromRecordState, snapshotFromRecordState } from './record-sync.js'
import { createRedisPubSub } from './pubsub.js'
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
  messageWindowMs: 10_000,
  maxMessagesPerWindow: 120,
  operationCompactionThreshold: 1000,
  operationCompactionKeepLast: 100,
  redisUrl: '',
  redisPrefix: 'projectoc:sync',
  instanceId: '',
  schemaVersion: 'oc-tldraw-1',
  protocolVersion: 'tldraw-sync-v2',
  officialCrdtEnabled: false,
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
    messageWindowMs: Math.max(1000, Number(process.env.SYNC_MESSAGE_WINDOW_MS || DEFAULTS.messageWindowMs)),
    maxMessagesPerWindow: Math.max(10, Number(process.env.SYNC_MAX_MESSAGES_PER_WINDOW || DEFAULTS.maxMessagesPerWindow)),
    operationCompactionThreshold: Math.max(0, Number(process.env.SYNC_OPERATION_COMPACTION_THRESHOLD || DEFAULTS.operationCompactionThreshold)),
    operationCompactionKeepLast: Math.max(0, Number(process.env.SYNC_OPERATION_COMPACTION_KEEP_LAST || DEFAULTS.operationCompactionKeepLast)),
    redisUrl: process.env.SYNC_REDIS_URL || process.env.REDIS_URL || DEFAULTS.redisUrl,
    redisPrefix: process.env.SYNC_REDIS_PREFIX || DEFAULTS.redisPrefix,
    instanceId: process.env.SYNC_INSTANCE_ID || crypto.randomUUID(),
    schemaVersion: process.env.TLDRAW_SCHEMA_VERSION || DEFAULTS.schemaVersion,
    protocolVersion: process.env.SYNC_PROTOCOL_VERSION || DEFAULTS.protocolVersion,
    officialCrdtEnabled: process.env.TLDRAW_SYNC_OFFICIAL_ENABLED === '1',
    officialSchemaVersion: process.env.TLDRAW_SCHEMA_VERSION || DEFAULTS.officialSchemaVersion,
    roomLeaseTtlMs: Math.max(5_000, Number(process.env.SYNC_ROOM_LEASE_TTL_MS || DEFAULTS.roomLeaseTtlMs)),
    officialDataDir: process.env.TLDRAW_SYNC_DATA_DIR || undefined,
    ...options,
  }
  const rooms = new Map()
  const fetchImpl = options.fetchImpl || fetch
  const allowedOrigins = parseAllowedOrigins(config.allowedOrigins)
  const pubsub = options.pubsub ?? createRedisPubSub({ url: config.redisUrl, prefix: config.redisPrefix, instanceId: config.instanceId })
  const instanceId = pubsub?.instanceId || config.instanceId
  const roomLease = options.roomLease ?? createRoomLease({ url: config.redisUrl, prefix: config.redisPrefix, instanceId, ttlMs: config.roomLeaseTtlMs })
  const officialRooms = options.officialRooms ?? new OfficialRoomManager({
    dataDir: config.officialDataDir,
    schemaVersion: config.officialSchemaVersion,
    loadLegacySnapshot: async roomKey => {
      const canvas = String(roomKey).split(':').at(-1)
      const state = await loadState(canvas)
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

  const send = (ws, value) => { if (ws.readyState === ws.OPEN) ws.send(JSON.stringify(value)) }
  const broadcast = (room, value, except) => { for (const client of room.clients) if (client !== except) send(client.ws, value) }
  const roomChannel = canvas => `${config.redisPrefix}:room:${canvas}`
  const roomStateVector = room => Object.fromEntries(
    [...(room.recordState?.stateVector || new Map()).entries()].sort(([left], [right]) => left.localeCompare(right)),
  )

  // A new canvas may have an intentionally minimal PostgreSQL snapshot because
  // TLDraw creates its root/page records in the client store.  Record-level
  // operations alone cannot reconstruct those implicit records, so the first
  // editor must establish an authoritative full snapshot before incremental
  // operations are accepted.
  const snapshotHasRecords = snapshot => Boolean(snapshot?.document?.store && Object.keys(snapshot.document.store).length)

  function rememberEvent(room, eventId) {
    if (!eventId) return false
    if (room.seenEvents.has(eventId)) return false
    room.seenEvents.add(eventId)
    if (room.seenEvents.size > 2_000) room.seenEvents.delete(room.seenEvents.values().next().value)
    return true
  }

  function collaborator(client) {
    return {id: client.id, username: client.claims.username, role: client.claims.role, ...client.presence}
  }

  function collaborators(room) {
    return [
      ...[...room.clients].map(collaborator),
      ...room.remoteClients.values(),
    ]
  }

  async function publishRoom(room, type, payload) {
    if (!pubsub) return
    const event = {
      version: 1,
      id: `${instanceId}:${crypto.randomUUID()}`,
      instance_id: instanceId,
      canvas: room.canvas,
      type,
      payload,
    }
    rememberEvent(room, event.id)
    try {
      await pubsub.publish(roomChannel(room.canvas), event)
    } catch (error) {
      room.pubsubLastError = String(error)
      // Redis pub/sub is deliberately best effort. The durable operation log
      // and cursor catch-up remain authoritative when a publication is missed.
    }
  }

  async function publishPresence(room, event, value) {
    await publishRoom(room, 'presence', {event, collaborator: value})
  }

  function officialRoomKey(claims, branch) {
    return `${claims.workspace}:${branch}:${claims.canvas}`
  }

  async function acquireOfficialRoom(claims, branch) {
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

  async function releaseOfficialRoom(roomKey) {
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

  async function appendOperations(canvas, operations) {
    const response = await fetchImpl(`${config.djangoUrl}/api/v1/canvases/${canvas}/sync-ops/`, {
      method: 'POST',
      headers: {'content-type': 'application/json', 'X-Sync-Internal-Secret': config.internalSecret},
      body: JSON.stringify({operations}),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.detail || `operation log append failed (${response.status})`)
    return body
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

  async function compactOperations(room, through) {
    if (!config.operationCompactionThreshold || !Number.isSafeInteger(Number(through))) return
    const compactedThrough = Number(room.operationCompactedThrough || 0)
    if (Number(through) - compactedThrough < config.operationCompactionThreshold) return
    const response = await fetchImpl(`${config.djangoUrl}/api/v1/canvases/${room.canvas}/compact-ops/`, {
      method: 'POST',
      headers: {'content-type': 'application/json', 'X-Sync-Internal-Secret': config.internalSecret},
      body: JSON.stringify({through: Number(through), keep_last: config.operationCompactionKeepLast}),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.detail || `operation log compaction failed (${response.status})`)
    if (Number.isSafeInteger(Number(body.compacted_through))) room.operationCompactedThrough = Number(body.compacted_through)
  }

  function scheduleLogRetry(room) {
    if (room.logRetryTimer || !room.logQueue.length) return
    const attempt = Math.min(room.logRetryAttempt || 0, 6)
    const delay = Math.min(30_000, 250 * (2 ** attempt))
    room.logRetryTimer = setTimeout(() => {
      room.logRetryTimer = undefined
      void flushOperationLog(room).catch(() => {})
    }, delay)
  }

  async function flushOperationLog(room) {
    if (room.logFlushing) return room.logFlushing
    room.logFlushing = (async () => {
      while (room.logQueue.length) {
        const entry = room.logQueue[0]
        try {
          const body = await appendOperations(room.canvas, entry.operations)
          const durableEntries = Array.isArray(body.operations) && body.operations.length
            ? body.operations
            : entry.operations.map((operation, index) => ({sequence: room.operationCursor + index + 1, operation}))
          room.operationCursor = Number.isSafeInteger(Number(body.cursor)) ? Number(body.cursor) : Math.max(room.operationCursor, ...durableEntries.map(item => Number(item.sequence) || 0))
          room.logQueue.shift()
          room.logRetryAttempt = 0
          const durableOps = durableEntries.map(item => item.operation || item).filter(Boolean)
          if (durableOps.length) {
            const event = {type: 'ops', ops: durableOps, version: room.version, operation_cursor: room.operationCursor, state_vector: roomStateVector(room), durable: true, source: entry.client.id}
            broadcast(room, event)
            void publishRoom(room, 'ops', event)
          }
          send(entry.client.ws, {
            type: 'ops_ack',
            accepted: durableOps.map(operation => `${operation.clientId}:${operation.opId}`),
            rejected: [],
            version: room.version,
            operation_cursor: room.operationCursor,
            durable: true,
          })
        } catch (error) {
          room.lastLogError = String(error)
          room.logRetryAttempt = (room.logRetryAttempt || 0) + 1
          scheduleLogRetry(room)
          throw error
        }
      }
      if (room.dirty) void saveState(room)
    })()
    void room.logFlushing.finally(() => {
      room.logFlushing = undefined
      if (room.logQueue.length) scheduleLogRetry(room)
    }).catch(() => {})
    return room.logFlushing
  }

  async function sendReplay(room, ws, after) {
    let cursor = after
    let hasMore = true
    try {
      while (hasMore) {
        const body = await loadOperations(room.canvas, cursor)
        const operations = Array.isArray(body.operations) ? body.operations : []
        send(ws, {type: 'ops_replay', after: cursor, cursor: body.cursor ?? room.operationCursor, operation_cursor: body.cursor ?? room.operationCursor, ops: operations.map(item => item.operation || item)})
        const lastSequence = operations.at(-1)?.sequence
        hasMore = Boolean(body.has_more && Number.isSafeInteger(Number(lastSequence)))
        if (hasMore) cursor = Number(lastSequence)
      }
    } catch (error) {
      send(ws, {type: 'error', code: 'replay_unavailable', detail: String(error)})
    }
  }

  function materialize(room) {
    // Keep the lifecycle marker outside the record-sync metadata. A freshly
    // created TLDraw canvas can have an empty store, so the marker is what
    // tells a later connection that a complete client-side baseline has been
    // established and that it may safely consume records-v1 deltas.
    const baselineReady = Boolean(room.syncMetadata?.baseline_ready || room.baselineReady)
    room.snapshot = snapshotFromRecordState(room.baseSnapshot, room.recordState)
    room.syncMetadata = metadataFromRecordState(room.recordState)
    if (baselineReady) room.syncMetadata.baseline_ready = true
  }

  function replaceRoomState(room, snapshot, version, syncMetadata = {}, operationCursor = room.operationCursor) {
    room.baseSnapshot = snapshot || {}
    // Seed materialization with the persisted lifecycle metadata. In
    // particular, baseline_ready must survive rebuilding record state; it is
    // not part of the field-version/tombstone metadata generated below.
    room.syncMetadata = syncMetadata && typeof syncMetadata === 'object' ? {...syncMetadata} : {}
    room.recordState = createRecordState(room.baseSnapshot, syncMetadata)
    materialize(room)
    room.version = version || 1
    if (Number.isSafeInteger(Number(operationCursor))) room.operationCursor = Number(operationCursor)
  }

  function applyDurableOperations(room, operations, {version, operationCursor, announce = true, source} = {}) {
    if (!Array.isArray(operations) || !operations.length) {
      if (Number.isSafeInteger(Number(operationCursor))) room.operationCursor = Math.max(room.operationCursor, Number(operationCursor))
      if (Number.isSafeInteger(Number(version))) room.version = Math.max(room.version, Number(version))
      return {accepted: [], rejected: [], deferred: [], conflicts: []}
    }
    const result = applyOperations(room.recordState, operations)
    if (Number.isSafeInteger(Number(operationCursor))) room.operationCursor = Math.max(room.operationCursor, Number(operationCursor))
    if (Number.isSafeInteger(Number(version))) room.version = Math.max(room.version, Number(version))
    if (result.accepted.length) {
      materialize(room)
      if (announce) broadcast(room, {type: 'ops', ops: result.accepted, version: room.version, operation_cursor: room.operationCursor, state_vector: roomStateVector(room), durable: true, source})
    }
    // A pub/sub event can arrive before an earlier durable event on another
    // instance. Replaying the authoritative log lets the room resolve causal
    // gaps instead of silently discarding a dependent operation.
    if (result.deferred.length && source !== 'replay') void catchUpRoom(room).catch(() => {})
    return result
  }

  async function catchUpRoom(room) {
    let cursor = room.operationCursor
    let hasMore = true
    while (hasMore) {
      const body = await loadOperations(room.canvas, cursor)
      const rows = Array.isArray(body.operations) ? body.operations : []
      const operations = rows.map(item => item.operation || item).filter(Boolean)
      applyDurableOperations(room, operations, {operationCursor: body.cursor, announce: false, source: 'replay'})
      const lastSequence = rows.at(-1)?.sequence
      hasMore = Boolean(body.has_more && Number.isSafeInteger(Number(lastSequence)))
      if (hasMore) cursor = Number(lastSequence)
      else if (Number.isSafeInteger(Number(body.cursor))) cursor = Number(body.cursor)
      if (!rows.length) break
    }
  }

  async function handlePubSubEvent(room, message) {
    if (!message || message.instance_id === instanceId || message.canvas !== room.canvas || !rememberEvent(room, message.id)) return
    const payload = message.payload || {}
    if (message.type === 'ops') {
      applyDurableOperations(room, payload.ops, {
        version: payload.version,
        operationCursor: payload.operation_cursor,
        source: payload.source,
      })
      return
    }
    if (message.type === 'persisted') {
      if (Number.isSafeInteger(Number(payload.version))) room.version = Math.max(room.version, Number(payload.version))
      if (Number.isSafeInteger(Number(payload.operation_cursor))) room.operationCursor = Math.max(room.operationCursor, Number(payload.operation_cursor))
      broadcast(room, {type: 'persisted', version: room.version, operation_cursor: room.operationCursor, source: 'remote-instance'})
      return
    }
    if (message.type === 'snapshot') {
      replaceRoomState(room, payload.snapshot, payload.version, payload.sync_metadata || {}, payload.operation_cursor)
      room.dirty = false
      broadcast(room, {type: 'snapshot', snapshot: room.snapshot, sync_metadata: room.syncMetadata, version: room.version, operation_cursor: room.operationCursor, source: 'remote-instance'})
      return
    }
    if (message.type === 'presence') {
      const person = payload.collaborator
      if (!person?.id) return
      if (payload.event === 'left') room.remoteClients.delete(person.id)
      else room.remoteClients.set(person.id, person)
      broadcast(room, {type: 'presence', event: payload.event || 'update', collaborator: person})
    }
  }

  async function subscribeRoom(room) {
    if (room.unsubscribe) return
    try {
      if (pubsub) {
        room.unsubscribe = await pubsub.subscribe(roomChannel(room.canvas), message => {
          void handlePubSubEvent(room, message)
        })
        room.pubsubStatus = 'ready'
      } else {
        room.pubsubStatus = 'disabled'
      }
      // Durable operation replay is required even in single-instance mode.
      // Redis only propagates live events between processes; it must not be
      // the condition that makes a restarted process reconstruct its room.
      await catchUpRoom(room)
    } catch (error) {
      room.pubsubStatus = 'error'
      room.pubsubLastError = String(error)
      // A failed subscription degrades to single-instance mode. Durable log
      // replay keeps a later connection from losing operations.
      await catchUpRoom(room).catch(replayError => {
        room.pubsubLastError = `${room.pubsubLastError}; replay: ${String(replayError)}`
      })
    }
  }

  function scheduleSave(room, delay = 1000) {
    if (room.retryTimer || !room.dirty) return
    room.retryTimer = setTimeout(() => {
      room.retryTimer = undefined
      void saveState(room)
    }, delay)
  }

  async function saveState(room) {
    if (room.saving || !room.dirty || !room.snapshot) return
    room.saving = true
    room.dirty = false
    // Capture the exact materialization sent to Django. Operations can arrive
    // while the request is in flight; never mark those newer records as part
    // of the persisted base by reading room.snapshot after the await.
    const sentSnapshot = room.snapshot
    const sentMetadata = room.syncMetadata
    const sentVersion = room.version
    const sentCursor = room.operationCursor
    try {
      const response = await fetchImpl(`${config.djangoUrl}/api/v1/canvases/${room.canvas}/sync-state/`, {
        method: 'POST', headers: {'content-type': 'application/json', 'X-Sync-Internal-Secret': config.internalSecret},
        body: JSON.stringify({snapshot: sentSnapshot, sync_metadata: sentMetadata, expected_version: sentVersion, operation_cursor: sentCursor}),
      })
      const body = await response.json().catch(() => ({}))
      if (response.ok) {
        const savedVersion = Number(body.snapshot_version)
        if (Number.isSafeInteger(savedVersion)) room.version = savedVersion
        if (Number.isSafeInteger(Number(body.operation_cursor))) room.operationCursor = Math.max(room.operationCursor, Number(body.operation_cursor))
        if (Number.isSafeInteger(Number(body.operation_compacted_through))) room.operationCompactedThrough = Number(body.operation_compacted_through)
        room.baseSnapshot = sentSnapshot
        room.lastError = undefined
        // A durable operation may have changed the live materialization while
        // this snapshot request was pending. Keep that newer state dirty and
        // let the next save persist it; only the captured snapshot is now the
        // database base.
        const changedWhileSaving = room.snapshot !== sentSnapshot || room.operationCursor > sentCursor || room.dirty
        if (changedWhileSaving) room.dirty = true
        const event = {type: 'persisted', version: room.version, operation_cursor: room.operationCursor, state_vector: roomStateVector(room)}
        broadcast(room, event)
        void publishRoom(room, 'persisted', event)
        // Compaction is deliberately best effort. A failed request leaves the
        // durable log intact and will be retried on a later successful save.
        void compactOperations(room, sentCursor).catch(error => { room.lastCompactionError = String(error) })
      } else if (response.status === 409 && body.snapshot) {
        const latestVersion = Number(body.snapshot_version)
        const keepLiveState = room.clients.size > 0 || room.dirty || room.snapshot !== sentSnapshot || room.logQueue.length > 0
        if (keepLiveState) {
          // Django's snapshot can be older than the in-memory operation log
          // (for example a legacy REST SaveQueue raced the websocket). It is
          // unsafe to broadcast that response: doing so makes every client
          // delete records that were already accepted in the room. Advance the
          // optimistic version only, retain the live record state, and retry
          // the current materialization against the new base version.
          if (Number.isSafeInteger(latestVersion)) room.version = latestVersion
          if (Number.isSafeInteger(Number(body.operation_cursor))) room.operationCursor = Math.max(room.operationCursor, Number(body.operation_cursor))
          if (Number.isSafeInteger(Number(body.operation_compacted_through))) room.operationCompactedThrough = Number(body.operation_compacted_through)
          room.lastError = 'snapshot version conflict; retained live collaboration state'
          room.dirty = true
          scheduleSave(room, 50)
        } else {
          replaceRoomState(room, body.snapshot, body.snapshot_version, body.sync_metadata || {}, body.operation_cursor)
          room.dirty = false
          const event = {type: 'snapshot', snapshot: room.snapshot, sync_metadata: room.syncMetadata, version: room.version, operation_cursor: room.operationCursor, source: 'server'}
          broadcast(room, event)
          void publishRoom(room, 'snapshot', event)
        }
      } else {
        room.lastError = body.detail || `snapshot save failed (${response.status})`
        room.dirty = true
        scheduleSave(room)
      }
    } catch (error) {
      room.lastError = String(error)
      room.dirty = true
      scheduleSave(room)
    } finally {
      room.saving = false
      if (room.dirty) void saveState(room)
    }
  }

  async function getRoom(canvas) {
    let room = rooms.get(canvas)
    if (room) return room
    room = {canvas, clients: new Set(), remoteClients: new Map(), seenEvents: new Set(), snapshot: null, syncMetadata: {}, baseSnapshot: null, recordState: null, baselineReady: false, version: 0, operationCursor: 0, operationCompactedThrough: 0, loading: null, saving: false, dirty: false, retryTimer: undefined, logQueue: [], logFlushing: undefined, logRetryTimer: undefined, logRetryAttempt: 0, lastLogError: undefined, lastCompactionError: undefined, unsubscribe: undefined, pubsubStatus: pubsub ? 'connecting' : 'disabled', pubsubLastError: undefined}
    rooms.set(canvas, room)
    room.loading = loadState(canvas).then(async state => {
      replaceRoomState(room, state.snapshot || {}, state.snapshot_version || 1, state.sync_metadata || {}, state.operation_cursor)
      if (Number.isSafeInteger(Number(state.operation_compacted_through))) room.operationCompactedThrough = Number(state.operation_compacted_through)
      room.baselineReady = Boolean(state.sync_metadata?.baseline_ready) || snapshotHasRecords(state.snapshot) || Number(state.operation_cursor || 0) > 0
      await subscribeRoom(room)
      return room
    }).catch(error => { rooms.delete(canvas); throw error })
    await room.loading
    return room
  }

  const server = http.createServer((req, res) => {
    if (req.url === '/health' || req.url === '/health/') return json(res, 200, {ok: true, rooms: rooms.size, official_rooms: officialRooms.health(), reconciliation: officialRooms.healthSummary(), room_leases: {active: officialLeases.size, health: roomLease.health}, max_room_clients: config.maxRoomClients, instance_id: instanceId, protocol: config.protocolVersion, schema_version: config.schemaVersion, official_crdt: {enabled: config.officialCrdtEnabled, protocol: OFFICIAL_PROTOCOL, schema_version: config.officialSchemaVersion}, capabilities: ['records-v1', 'tldraw-sync-v2', 'causal-ops', 'offline-replay'], pubsub: pubsub?.health || {mode: 'disabled', status: 'disabled'}})
    return json(res, 404, {detail: 'not found'})
  })
  const wss = new WebSocketServer({ noServer: true, maxPayload: config.maxSnapshotBytes + 1024 * 1024 })
  server.on('upgrade', async (req, socket, head) => {
    try {
      if (!originAllowed(req, allowedOrigins, config.requireOrigin)) return rejectUpgrade(socket, 403, 'Forbidden origin')
      const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`)
      const match = url.pathname.match(/^\/rooms\/([^/]+)$/)
      const claims = verifyTicket(url.searchParams.get('ticket'), config.ticketSecret)
      const requestedProtocol = url.searchParams.get('protocol') || 'records-v1'
      const requestedSchema = url.searchParams.get('schema') || (requestedProtocol === OFFICIAL_PROTOCOL ? config.officialSchemaVersion : config.schemaVersion)
      if (!['records-v1', OFFICIAL_PROTOCOL].includes(requestedProtocol)) throw new Error('unsupported protocol')
      if (!match || claims.canvas !== match[1]) throw new Error('ticket room mismatch')
      if (requestedProtocol === OFFICIAL_PROTOCOL) {
        if (!config.officialCrdtEnabled) return rejectUpgrade(socket, 426, 'Official TLDraw CRDT is not enabled')
        if (requestedSchema !== config.officialSchemaVersion) return rejectUpgrade(socket, 409, 'TLDraw schema upgrade required')
        const branch = url.searchParams.get('branch') || claims.branch
        if (!branch || !claims.branch || claims.branch !== branch) return rejectUpgrade(socket, 403, 'Branch claim required')
        const {roomKey, state} = await acquireOfficialRoom(claims, branch)
        if (state.room.getNumActiveSessions() >= config.maxRoomClients) return rejectUpgrade(socket, 429, 'Room is full')
        wss.handleUpgrade(req, socket, head, ws => wss.emit('official-connection', ws, req, state, roomKey, claims, requestedSchema))
        return
      }
      if (requestedSchema !== config.schemaVersion) throw new Error('schema mismatch')
      const room = await getRoom(claims.canvas)
      if (room.clients.size >= config.maxRoomClients) return rejectUpgrade(socket, 429, 'Room is full')
      const afterValue = url.searchParams.get('after')
      const after = afterValue === null ? null : Math.max(0, Number.parseInt(afterValue, 10) || 0)
      wss.handleUpgrade(req, socket, head, ws => wss.emit('connection', ws, req, room, claims, after, requestedProtocol, requestedSchema))
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

  wss.on('connection', (ws, req, room, claims, after = null, requestedProtocol = 'records-v1', requestedSchema = config.schemaVersion) => {
    const client = {ws, claims, id: claims.client_id, protocol: requestedProtocol, schemaVersion: requestedSchema, messageTimes: [], isAlive: true, presence: {}}
    room.clients.add(client)
    ws.on('pong', () => { client.isAlive = true })
    send(ws, {type: 'hello', protocol: requestedProtocol, schema_version: config.schemaVersion, capabilities: ['records-v1', 'tldraw-sync-v2', 'causal-ops', 'offline-replay'], canvas: room.canvas, snapshot: after === null ? room.snapshot : null, needs_baseline: !room.baselineReady, replay_from: after, version: room.version, operation_cursor: room.operationCursor, state_vector: roomStateVector(room), role: claims.role, collaborators: collaborators(room)})
    if (after !== null) void sendReplay(room, ws, after)
    const joined = collaborator(client)
    broadcast(room, {type: 'presence', event: 'joined', collaborator: joined}, ws)
    void publishPresence(room, 'joined', joined)
    ws.on('message', async raw => {
      if (!acceptMessage(client, Date.now(), config.messageWindowMs, config.maxMessagesPerWindow)) return send(ws, {type: 'error', code: 'rate_limited', detail: '消息过于频繁，请稍后重试'})
      let message
      try { message = JSON.parse(raw.toString()) } catch { return send(ws, {type: 'error', detail: 'invalid JSON'}) }
      if (message.type === 'ping') return send(ws, {type: 'pong', at: Date.now()})
      if (message.type === 'presence') {
        const presence = sanitizePresence(message.state)
        client.presence = {...client.presence, ...presence}
        const updated = collaborator(client)
        broadcast(room, {type: 'presence', event: 'update', collaborator: updated}, ws)
        void publishPresence(room, 'update', updated)
      }
      if (message.type === 'baseline') {
        if (claims.role === 'reader') return send(ws, {type: 'error', code: 'read_only', detail: 'reader cannot initialize a canvas'})
        if (room.baselineReady || room.operationCursor > 0 || room.logQueue.length) {
          return send(ws, {type: 'baseline_ack', accepted: false, reason: 'baseline_exists', snapshot: room.snapshot, version: room.version, operation_cursor: room.operationCursor})
        }
        if (!message.snapshot || typeof message.snapshot !== 'object' || Array.isArray(message.snapshot)) return send(ws, {type: 'error', detail: 'baseline snapshot must be an object'})
        const snapshotBytes = Buffer.byteLength(JSON.stringify(message.snapshot), 'utf8')
        if (snapshotBytes > config.maxSnapshotBytes) return send(ws, {type: 'error', code: 'snapshot_too_large', detail: `snapshot exceeds ${config.maxSnapshotBytes} bytes`})
        replaceRoomState(room, message.snapshot, room.version || 1, {...(message.sync_metadata || {}), baseline_ready: true})
        room.baselineReady = true
        room.dirty = true
        const event = {type: 'snapshot', snapshot: room.snapshot, sync_metadata: room.syncMetadata, version: room.version, operation_cursor: room.operationCursor, source: client.id}
        broadcast(room, event, ws)
        void publishRoom(room, 'snapshot', event)
        void saveState(room)
        return send(ws, {type: 'baseline_ack', accepted: true, version: room.version, operation_cursor: room.operationCursor, state_vector: roomStateVector(room)})
      }
      if (message.type === 'ops') {
        if (claims.role === 'reader') return send(ws, {type: 'error', code: 'read_only', detail: 'reader cannot edit'})
        if (!Array.isArray(message.ops) || message.ops.length > 100) return send(ws, {type: 'error', code: 'invalid_operations', detail: 'ops must contain at most 100 operations'})
        const operations = message.ops.map(operation => ({...operation, clientId: operation?.clientId || client.id}))
        if (operations.some(operation => operation.clientId !== client.id)) return send(ws, {type: 'error', code: 'operation_actor_mismatch', detail: 'operation clientId does not match the signed ticket'})
        const result = applyOperations(room.recordState, operations)
        if (result.accepted.length) {
          materialize(room)
          room.dirty = true
          const entry = {operations: result.accepted, client}
          room.logQueue.push(entry)
          try {
            await flushOperationLog(room)
            return send(ws, {type: 'ops_ack', accepted: result.accepted.map(op => `${op.clientId}:${op.opId}`), rejected: result.rejected, deferred: result.deferred, conflicts: result.conflicts, version: room.version, operation_cursor: room.operationCursor, state_vector: roomStateVector(room), durable: true, snapshot: result.rejected.length ? room.snapshot : undefined})
          } catch {
            return send(ws, {type: 'ops_ack', accepted: result.accepted.map(op => `${op.clientId}:${op.opId}`), rejected: result.rejected, deferred: result.deferred, conflicts: result.conflicts, version: room.version, operation_cursor: room.operationCursor, state_vector: roomStateVector(room), durable: false, error: room.lastLogError, snapshot: result.rejected.length ? room.snapshot : undefined})
          }
        }
        return send(ws, {type: 'ops_ack', accepted: [], rejected: result.rejected, deferred: result.deferred, conflicts: result.conflicts, version: room.version, operation_cursor: room.operationCursor, state_vector: roomStateVector(room), durable: true, snapshot: result.rejected.length ? room.snapshot : undefined})
      }
      if (message.type === 'snapshot') {
        if (claims.role === 'reader') return send(ws, {type: 'error', code: 'read_only', detail: 'reader cannot edit'})
        if (!message.snapshot || typeof message.snapshot !== 'object' || Array.isArray(message.snapshot)) return send(ws, {type: 'error', detail: 'snapshot must be an object'})
        const snapshotBytes = Buffer.byteLength(JSON.stringify(message.snapshot), 'utf8')
        if (snapshotBytes > config.maxSnapshotBytes) return send(ws, {type: 'error', code: 'snapshot_too_large', detail: `snapshot exceeds ${config.maxSnapshotBytes} bytes`})
        replaceRoomState(room, message.snapshot, room.version, {...(message.sync_metadata || {}), protocol: client.protocol, schema_version: client.schemaVersion, baseline_ready: true})
        room.baselineReady = true
        room.dirty = true
        const event = {type: 'snapshot', snapshot: room.snapshot, sync_metadata: room.syncMetadata, version: room.version, operation_cursor: room.operationCursor, source: client.id}
        broadcast(room, event)
        void publishRoom(room, 'snapshot', event)
        void saveState(room)
      }
    })
    ws.on('close', () => {
      room.clients.delete(client)
      const left = {id: client.id, username: claims.username, role: claims.role}
      broadcast(room, {type: 'presence', event: 'left', collaborator: left}, ws)
      void publishPresence(room, 'left', left)
      if (!room.clients.size && !room.saving && !room.dirty) {
        if (room.retryTimer) clearTimeout(room.retryTimer)
        if (room.unsubscribe) void room.unsubscribe().catch(() => {})
        rooms.delete(room.canvas)
      }
    })
  })

  const heartbeat = setInterval(() => {
    for (const room of rooms.values()) {
      for (const client of room.clients) {
        if (!client.isAlive) { client.ws.terminate(); continue }
        client.isAlive = false
        client.ws.ping()
      }
    }
  }, 30_000)
  heartbeat.unref()

  async function close() {
    clearInterval(heartbeat)
    for (const room of rooms.values()) {
      if (room.retryTimer) clearTimeout(room.retryTimer)
      if (room.logRetryTimer) clearTimeout(room.logRetryTimer)
      if (room.unsubscribe) await room.unsubscribe().catch(() => {})
      for (const client of room.clients) client.ws.close()
    }
    await new Promise(resolve => server.close(() => resolve()))
    await new Promise(resolve => wss.close(() => resolve()))
    for (const roomKey of [...officialLeases.keys()]) await releaseOfficialRoom(roomKey)
    officialRooms.close()
    if (pubsub) await pubsub.close().catch(() => {})
    await roomLease.close().catch(() => {})
    rooms.clear()
  }

  return {server, wss, rooms, config, listen: (...args) => server.listen(...args), close}
}

export function startSyncServer() {
  const app = createSyncServer()
  app.listen(app.config.port, '0.0.0.0', () => console.log(`ProjectOC sync service listening on ${app.config.port}`))
  return app
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) startSyncServer()
