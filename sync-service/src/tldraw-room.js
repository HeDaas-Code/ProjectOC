import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import {
  NodeSqliteWrapper,
  SQLiteSyncStorage,
  TLSocketRoom,
} from '@tldraw/sync-core'
import { createTLSchema, defaultBindingSchemas, defaultShapeSchemas } from '@tldraw/tlschema'
import { T } from '@tldraw/validate'

/**
 * Official tldraw sync-core room integration.
 *
 * records-v1 is retired. Historical snapshots may be imported once. This
 * module owns tldraw-sync-v2 rooms and keeps SQLite as a
 * protocol journal/cache; the caller is responsible for persisting the
 * materialized snapshot to PostgreSQL.
 */

export const OFFICIAL_PROTOCOL = 'tldraw-sync-v2'
export const OFFICIAL_SCHEMA_VERSION = 'oc-tldraw-2'

const customShapeSchemas = {
  ...defaultShapeSchemas,
  'oc-card': {
    props: {
      w: T.number,
      h: T.number,
      kind: T.string,
      text: T.string,
      proposalId: T.string,
    },
  },
  'oc-entity': {
    props: {
      w: T.number,
      h: T.number,
      entityId: T.string,
      title: T.string,
      entityType: T.string,
      stale: T.boolean,
    },
  },
}

export const projectocSchema = createTLSchema({
  shapes: customShapeSchemas,
  bindings: defaultBindingSchemas,
})

function safeRoomName(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, 48)
}

export function stableHash(value) {
  return crypto.createHash('sha256').update(JSON.stringify(value ?? {}, (_key, item) => item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b)))
    : item)).digest('hex')
}

function durableSnapshotValue(value) {
  if (value && value.snapshot && typeof value.snapshot === 'object' && !Array.isArray(value.snapshot)
      && (Object.hasOwn(value, 'snapshot_hash') || Object.hasOwn(value, 'latest_sync_event') || Object.hasOwn(value, 'sync_metadata'))) return value.snapshot
  return value
}

function legacyToStoreSnapshot(snapshot) {
  const durable = durableSnapshotValue(snapshot)
  if (durable?.official_snapshot?.documents && Array.isArray(durable.official_snapshot.documents)) {
    return durable.official_snapshot
  }
  const document = durable?.document && typeof durable.document === 'object'
    ? durable.document
    : durable
  const store = document?.store && typeof document.store === 'object' ? document.store : {}
  // records-v1 snapshots carried the client schema blob, which may be a
  // partial/older schema and is not safe to feed into StoreSchema migration.
  // The room schema registry is the single negotiated schema for official
  // rooms; legacy records are migrated against it instead.
  return { store, schema: projectocSchema.serialize() }
}

export function roomSnapshotToLegacy(snapshot) {
  const store = Object.fromEntries((snapshot?.documents || []).map(({ state }) => [state.id, state]))
  return {
    // Keep the document projection for domain APIs and exports, and preserve
    // the complete official RoomSnapshot (including tombstones and clocks).
    // The latter is the only source used when an official room is rebuilt.
    official_snapshot: snapshot,
    document: {
      store,
      schema: snapshot?.schema || projectocSchema.serialize(),
    },
    session: {},
  }
}

function makeSocketAdapter(ws) {
  // ws@8 exposes addEventListener, but the small adapter makes this explicit
  // and also keeps the room usable with test doubles and older ws releases.
  return {
    readyState: ws.readyState,
    send: data => ws.send(data),
    close: (code, reason) => ws.close(code, reason),
    addEventListener: (type, listener) => {
      if (typeof ws.addEventListener === 'function') return ws.addEventListener(type, listener)
      ws.on(type, event => listener(event))
    },
    removeEventListener: (type, listener) => {
      if (typeof ws.removeEventListener === 'function') return ws.removeEventListener(type, listener)
      ws.off(type, listener)
    },
  }
}


function diffPuts(diff) {
  return diff?.puts || diff?.added || {}
}

function diffDeletes(diff) {
  if (Array.isArray(diff?.deletes)) return diff.deletes
  if (Array.isArray(diff?.removed)) return diff.removed
  if (diff?.removed && typeof diff.removed === 'object') return Object.keys(diff.removed)
  return []
}

/**
 * Apply one durable TLSync forward diff to a SQLiteSyncStorage transaction.
 * PostgreSQL stores the exact forward diff emitted by TLSyncRoom. A few early
 * snapshots used the records-v1 `added/removed` names, so accepting both
 * shapes keeps replay backwards compatible without weakening validation.
 */
export function applyDurableDiff(storage, diff, transactionId = 'durable-replay') {
  if (!diff || typeof diff !== 'object') throw new Error('durable diff must be an object')
  const puts = diffPuts(diff)
  const deletes = diffDeletes(diff)
  const result = storage.transaction(txn => {
    for (const [id, value] of Object.entries(puts)) {
      // TLSyncStorage forward diffs normally contain the complete record. If
      // a legacy producer persisted [before, after], only the after value is
      // safe to materialize.
      const record = Array.isArray(value) ? value.at(-1) : value
      if (!record || typeof record !== 'object' || record.id !== id || typeof record.typeName !== 'string') {
        throw new Error(`invalid durable put for ${id}`)
      }
      txn.set(id, record)
    }
    for (const id of deletes) {
      if (typeof id !== 'string' || !id) throw new Error('invalid durable delete id')
      txn.delete(id)
    }
  }, {id: transactionId})
  return result
}

async function replayDurableEvents(storage, events, fromClock, targetClock, expectedHash) {
  const ordered = [...(events || [])]
    .filter(event => Number(event?.document_clock) > Number(fromClock))
    .sort((a, b) => Number(a.document_clock) - Number(b.document_clock) || String(a.id || '').localeCompare(String(b.id || '')))
  if (!ordered.length) return {ok: false, reason: 'no durable events available', applied: 0}
  let applied = 0
  let currentClock = Number(fromClock)
  for (const event of ordered) {
    const eventClock = Number(event.document_clock)
    if (!Number.isSafeInteger(eventClock) || eventClock <= currentClock || eventClock > Number(targetClock)) {
      return {ok: false, reason: `non-monotonic durable event clock at ${eventClock}`, applied}
    }
    const result = applyDurableDiff(storage, event.diff, `durable-replay:${eventClock}:${event.id || applied}`)
    currentClock = Number(result.documentClock)
    if (currentClock !== eventClock) {
      return {ok: false, reason: `replay clock mismatch at ${eventClock} (actual ${currentClock})`, applied}
    }
    applied += 1
  }
  if (currentClock !== Number(targetClock)) return {ok: false, reason: `replay ended at ${currentClock}, expected ${targetClock}`, applied}
  const actualHash = stableHash(roomSnapshotToLegacy(storage.getSnapshot?.() || {}))
  if (expectedHash && actualHash !== expectedHash) return {ok: false, reason: `replay hash mismatch (actual ${actualHash}, expected ${expectedHash})`, applied}
  return {ok: true, reason: `replayed ${applied} durable event(s)`, applied, clock: currentClock, hash: actualHash}
}

export class OfficialRoomManager {
  constructor({
    dataDir = process.env.TLDRAW_SYNC_DATA_DIR || path.resolve(process.cwd(), '.tldraw-sync'),
    schema = projectocSchema,
    schemaVersion = OFFICIAL_SCHEMA_VERSION,
    loadLegacySnapshot,
    loadDurableEvents,
    persistSnapshot,
    persistReconciliation,
    onCommittedChanges,
    logger = console,
  } = {}) {
    this.dataDir = dataDir
    this.schema = schema
    this.schemaVersion = schemaVersion
    this.loadLegacySnapshot = loadLegacySnapshot
    this.loadDurableEvents = loadDurableEvents
    this.persistSnapshot = persistSnapshot
    this.persistReconciliation = persistReconciliation
    this.onCommittedChanges = onCommittedChanges
    this.logger = logger
    this.rooms = new Map()
    this.loadingRooms = new Map()
    fs.mkdirSync(dataDir, { recursive: true })
  }

  async getRoom(roomKey) {
    if (this.loadingRooms.has(roomKey)) return this.loadingRooms.get(roomKey)
    const loading = this.initializeRoom(roomKey)
    this.loadingRooms.set(roomKey, loading)
    try { return await loading } finally { this.loadingRooms.delete(roomKey) }
  }

  async initializeRoom(roomKey) {
    const existing = this.rooms.get(roomKey)
    if (existing) return existing

    const dbPath = path.join(this.dataDir, `${safeRoomName(roomKey)}.sqlite`)
    const durable = this.loadLegacySnapshot ? await this.loadLegacySnapshot(roomKey) : null
    let db = new DatabaseSync(dbPath)
    let sql = new NodeSqliteWrapper(db, { tablePrefix: 'tldraw_' })
    let storage
    let migratedFromLegacy = false
    let reconciliation = 'not_checked'
    let reconciliationDetail = ''
    let replayedEvents = 0
    const durableSnapshot = durableSnapshotValue(durable)
    const durableHash = durable?.snapshot_hash || stableHash(durableSnapshot)
    const durableClock = Number(durable?.latest_sync_event?.document_clock ?? durable?.sync_metadata?.document_clock ?? 0)
    const initialize = (snapshot) => new SQLiteSyncStorage({ sql, snapshot: snapshot ? legacyToStoreSnapshot(snapshot) : undefined })
    if (SQLiteSyncStorage.hasBeenInitialized(sql)) {
      storage = new SQLiteSyncStorage({ sql })
      const localLegacy = roomSnapshotToLegacy(storage.getSnapshot?.() || {})
      const localHash = stableHash(localLegacy)
      const localClock = Number(storage.getClock() || 0)
      const clockMatches = !durableClock || localClock === durableClock
      const hashMatches = !durableHash || durableHash === localHash
      if (durable && (!clockMatches || !hashMatches)) {
        let replayResult
        if (localClock < durableClock && this.loadDurableEvents) {
          try {
            const events = await this.loadDurableEvents(roomKey, localClock, durableClock)
            replayResult = await replayDurableEvents(storage, events, localClock, durableClock, durableHash)
          } catch (error) {
            replayResult = {ok: false, reason: `durable replay failed: ${error}`, applied: 0}
          }
        }
        if (replayResult?.ok) {
          reconciliation = 'replayed'
          replayedEvents = replayResult.applied || 0
          reconciliationDetail = replayResult.reason
        } else {
          // PostgreSQL is the durable domain boundary. A stale/corrupt SQLite
          // journal is discarded and rebuilt from the last confirmed snapshot;
          // subsequent commits will append fresh protocol history.
          try { db.close() } catch {}
          try { fs.unlinkSync(dbPath) } catch (error) { if (error.code !== 'ENOENT') throw error }
          db = new DatabaseSync(dbPath)
          sql = new NodeSqliteWrapper(db, { tablePrefix: 'tldraw_' })
          storage = initialize(durable)
          reconciliation = 'rebuilt'
          reconciliationDetail = replayResult
            ? `${replayResult.reason}; rebuilt from durable snapshot`
            : `sqlite clock/hash mismatch (local=${localClock}/${localHash}, durable=${durableClock}/${durableHash})`
        }
      } else {
        reconciliation = 'matched'
      }
    } else {
      storage = initialize(durable)
      migratedFromLegacy = Boolean(durable)
      reconciliation = durable ? 'rebuilt' : 'not_checked'
      reconciliationDetail = durable ? 'initialized from PostgreSQL snapshot' : ''
    }

    const roomState = {
      roomKey,
      dbPath,
      db,
      sql,
      storage,
      room: null,
      migratedFromLegacy,
      lastPersistedClock: storage.getClock(),
      lastError: null,
      persistQueue: Promise.resolve(),
      persistPending: [],
      persistRetryTimer: null,
      persistRetryAttempt: 0,
      persistInFlight: false,
      createdAt: new Date().toISOString(),
      durableDocumentClock: durableClock,
      durableSnapshotHash: durable?.snapshot_hash || stableHash(durableSnapshot),
      reconciliation,
      reconciliationDetail,
      replayedEvents,
    }
    const enqueuePersistence = ({ diff, documentClock, snapshot = storage.getSnapshot?.(), migratedFromLegacy = roomState.migratedFromLegacy } = {}) => {
      roomState.lastPersistedClock = documentClock
      if (!snapshot || !this.persistSnapshot) return false
      const payload = roomSnapshotToLegacy(snapshot)
      roomState.persistPending.push({
        payload,
        metadata: {
          protocol: OFFICIAL_PROTOCOL,
          schema_version: this.schemaVersion,
          document_clock: documentClock,
          diff,
          room_key: roomKey,
          source: 'sync-service',
          migrated_from_legacy: migratedFromLegacy,
        },
      })
      // The queue contains full snapshots, not only deltas. This means a
      // transient PostgreSQL outage cannot lose a committed CRDT revision;
      // every queued revision is retried in order and remains auditable.
      void pumpPersistence(roomState)
      return true
    }
    const room = new TLSocketRoom({
      schema: this.schema,
      storage,
      log: {
        warn: (...args) => this.logger.warn?.('[tldraw-room]', roomKey, ...args),
        error: (...args) => this.logger.error?.('[tldraw-room]', roomKey, ...args),
      },
      onCommittedChanges: ({ diff, documentClock }) => {
        const snapshot = storage.getSnapshot?.()
        if (!enqueuePersistence({ diff, documentClock, snapshot })) return
        try { this.onCommittedChanges?.({ roomKey, diff, documentClock, snapshot }) } catch (error) { this.logger.error?.(error) }
      },
    })
    const pumpPersistence = async state => {
      if (state.persistInFlight || !this.persistSnapshot) return
      state.persistInFlight = true
      try {
        while (state.persistPending.length) {
          const item = state.persistPending[0]
          try {
            const result = await this.persistSnapshot(state.roomKey, item.payload, item.metadata)
            state.persistPending.shift()
            state.persistRetryAttempt = 0
            state.durableDocumentClock = Number(item.metadata.document_clock) || state.durableDocumentClock
            state.durableSnapshotHash = stableHash(item.payload)
            if (item.metadata?.migrated_from_legacy) state.migratedFromLegacy = false
            state.reconciliation = 'matched'
            state.reconciliationDetail = ''
            state.lastError = null
            // Preserve the promise value for callers/tests that inspect the
            // old queue contract while keeping the durable retry queue alive.
            state.lastPersistResult = result
          } catch (error) {
            state.lastError = String(error)
            state.reconciliation = 'failed'
            state.reconciliationDetail = 'snapshot persistence pending retry'
            state.persistRetryAttempt = Math.min(8, state.persistRetryAttempt + 1)
            const delay = Math.min(30_000, 250 * (2 ** (state.persistRetryAttempt - 1)))
            if (!state.persistRetryTimer) {
              state.persistRetryTimer = setTimeout(() => {
                state.persistRetryTimer = null
                void pumpPersistence(state)
              }, delay)
              state.persistRetryTimer.unref?.()
            }
            this.logger.error?.('[tldraw-room] snapshot persistence failed; queued for retry', state.roomKey, error)
            break
          }
        }
      } finally {
        state.persistInFlight = false
      }
    }

    roomState.room = room
    roomState.pumpPersistence = pumpPersistence
    // Exposed for the durable recovery path and deterministic tests. Normal
    // callers should let TLSocketRoom invoke this through onCommittedChanges.
    roomState.enqueuePersistence = enqueuePersistence
    this.rooms.set(roomKey, roomState)
    if (this.persistReconciliation) {
      try {
        await this.persistReconciliation(roomKey, {
          status: roomState.reconciliation,
          detail: roomState.reconciliationDetail,
          document_clock: storage.getClock(),
          durable_document_clock: durableClock,
          durable_snapshot_hash: roomState.durableSnapshotHash,
          replayed_events: roomState.replayedEvents,
          protocol: OFFICIAL_PROTOCOL,
          schema_version: this.schemaVersion,
        })
      } catch (error) {
        roomState.lastError = `reconciliation persistence failed: ${error}`
        this.logger.warn?.('[tldraw-room] reconciliation persistence failed', roomKey, error)
      }
    }
    if (migratedFromLegacy && this.persistSnapshot) {
      // Legacy migration is durable work, but it must not make a room
      // unavailable when PostgreSQL is temporarily down. Queue the migrated
      // snapshot through the same ordered retry path as normal CRDT commits.
      // The queue marks migrated_from_legacy so a successful checkpoint can
      // clear the legacy flag without losing the audit metadata.
      enqueuePersistence({
        diff: { puts: {}, deletes: [] },
        documentClock: storage.getClock(),
        snapshot: storage.getSnapshot?.(),
        migratedFromLegacy: true,
      })
    }
    return roomState
  }

  connect(roomState, { sessionId, ws, isReadonly, meta }) {
    roomState.room.handleSocketConnect({
      sessionId,
      socket: makeSocketAdapter(ws),
      isReadonly,
      meta,
    })
  }

  closeRoom(roomKey, {code = 1000, reason = ''} = {}) {
    const state = this.rooms.get(roomKey)
    if (!state) return
    if (code !== 1000 || reason) {
      for (const session of state.room.sessions?.values?.() || []) {
        try { session.socket.close(code, reason) } catch {}
      }
    }
    if (state.persistRetryTimer) clearTimeout(state.persistRetryTimer)
    state.persistRetryTimer = null
    state.room.close()
    state.db.close()
    this.rooms.delete(roomKey)
  }

  close() {
    for (const key of [...this.rooms.keys()]) this.closeRoom(key)
  }

  health() {
    return [...this.rooms.values()].map(state => ({
      room: state.roomKey,
      active_sessions: state.room.getNumActiveSessions(),
      document_clock: state.storage.getClock(),
      schema_version: this.schemaVersion,
      protocol: OFFICIAL_PROTOCOL,
      sqlite: state.dbPath,
      last_error: state.lastError,
      durable_document_clock: state.durableDocumentClock,
      durable_snapshot_hash: state.durableSnapshotHash,
      reconciliation: state.reconciliation,
      reconciliation_detail: state.reconciliationDetail,
      replayed_events: state.replayedEvents,
      pending_persistence: state.persistPending.length,
      persistence_retry_attempt: state.persistRetryAttempt,
      persistence_in_flight: state.persistInFlight,
    }))
  }

  healthSummary() {
    const summary = { active_rooms: this.rooms.size, matched: 0, replayed: 0, rebuilt: 0, failed: 0, not_checked: 0 }
    for (const state of this.rooms.values()) {
      if (state.reconciliation === 'matched') summary.matched += 1
      else if (state.reconciliation === 'replayed') summary.replayed += 1
      else if (state.reconciliation === 'rebuilt') summary.rebuilt += 1
      else if (state.reconciliation === 'not_checked') summary.not_checked += 1
      else summary.failed += 1
      if (state.lastError) summary.failed += 1
    }
    return summary
  }
}
