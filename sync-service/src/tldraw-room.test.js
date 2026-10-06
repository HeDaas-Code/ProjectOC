import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { OfficialRoomManager, OFFICIAL_PROTOCOL, OFFICIAL_SCHEMA_VERSION, roomSnapshotToLegacy, stableHash } from './tldraw-room.js'

test('official room manager initializes a durable room and exposes reconciliation health', async t => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'projectoc-tldraw-room-'))
  t.after(() => fs.rmSync(dataDir, { recursive: true, force: true }))
  const manager = new OfficialRoomManager({ dataDir, loadLegacySnapshot: async () => ({ snapshot: {}, snapshot_hash: '', latest_sync_event: null }) })
  t.after(() => manager.close())

  const state = await manager.getRoom('workspace:main:canvas-1')
  assert.equal(state.roomKey, 'workspace:main:canvas-1')
  assert.equal(state.reconciliation, 'rebuilt')
  assert.equal(state.room.getNumActiveSessions(), 0)
  assert.deepEqual(manager.healthSummary(), { active_rooms: 1, matched: 0, replayed: 0, rebuilt: 1, failed: 0, not_checked: 0 })
  assert.equal(manager.health()[0].protocol, OFFICIAL_PROTOCOL)
  assert.equal(manager.health()[0].schema_version, OFFICIAL_SCHEMA_VERSION)
})

test('official room manager rebuilds SQLite journal when durable hash disagrees', async t => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'projectoc-tldraw-reconcile-'))
  t.after(() => fs.rmSync(dataDir, { recursive: true, force: true }))
  let durable = { snapshot: {}, snapshot_hash: '', latest_sync_event: null }
  const first = new OfficialRoomManager({ dataDir, loadLegacySnapshot: async () => durable })
  await first.getRoom('workspace:branch-a:canvas-2')
  first.close()

  durable = { snapshot: {}, snapshot_hash: 'deliberately-stale', latest_sync_event: { document_clock: 4 } }
  const second = new OfficialRoomManager({ dataDir, loadLegacySnapshot: async () => durable })
  t.after(() => second.close())
  const state = await second.getRoom('workspace:branch-a:canvas-2')
  assert.equal(state.reconciliation, 'rebuilt')
  assert.match(state.reconciliationDetail, /mismatch/)
  assert.equal(second.healthSummary().rebuilt, 1)
})

test('room legacy projection preserves official snapshot tombstones and clocks', () => {
  const snapshot = {
    documents: [{ state: { id: 'shape:1', typeName: 'shape', x: 10 } }],
    tombstones: [{ id: 'shape:gone', clock: 8 }],
    clock: 9,
    schema: { shapes: {} },
  }
  const legacy = roomSnapshotToLegacy(snapshot)
  assert.equal(legacy.document.store['shape:1'].x, 10)
  assert.deepEqual(legacy.official_snapshot.tombstones, snapshot.tombstones)
  assert.equal(legacy.official_snapshot.clock, 9)
})


test('official room manager replays durable forward diffs before rebuilding', async t => {
  const sourceDir = fs.mkdtempSync(path.join(os.tmpdir(), 'projectoc-tldraw-source-'))
  const targetDir = fs.mkdtempSync(path.join(os.tmpdir(), 'projectoc-tldraw-replay-'))
  t.after(() => {
    fs.rmSync(sourceDir, { recursive: true, force: true })
    fs.rmSync(targetDir, { recursive: true, force: true })
  })

  const source = new OfficialRoomManager({
    dataDir: sourceDir,
    loadLegacySnapshot: async () => ({ snapshot: {}, snapshot_hash: '', latest_sync_event: null }),
  })
  const sourceState = await source.getRoom('workspace:replay:canvas-1')
  sourceState.storage.transaction(txn => txn.set('custom:one', {id: 'custom:one', typeName: 'custom', value: 'replayed'}))
  const durableSnapshot = roomSnapshotToLegacy(sourceState.storage.getSnapshot())
  const durableEvent = {
    id: 'event-1',
    document_clock: 1,
    snapshot_hash: stableHash(durableSnapshot),
    diff: {puts: {'custom:one': {id: 'custom:one', typeName: 'custom', value: 'replayed'}}, deletes: []},
  }
  source.close()

  const empty = new OfficialRoomManager({
    dataDir: targetDir,
    loadLegacySnapshot: async () => ({ snapshot: {}, snapshot_hash: '', latest_sync_event: null }),
  })
  await empty.getRoom('workspace:replay:canvas-1')
  empty.close()

  const manager = new OfficialRoomManager({
    dataDir: targetDir,
    loadLegacySnapshot: async () => ({
      snapshot: durableSnapshot,
      snapshot_hash: durableEvent.snapshot_hash,
      latest_sync_event: {document_clock: 1},
    }),
    loadDurableEvents: async () => [durableEvent],
  })
  t.after(() => manager.close())
  const state = await manager.getRoom('workspace:replay:canvas-1')
  assert.equal(state.reconciliation, 'replayed')
  assert.equal(state.storage.getClock(), 1)
  assert.equal(state.storage.getSnapshot().documents.some(item => item.state.id === 'custom:one'), true)
  assert.equal(manager.health()[0].replayed_events, 1)
})

test('official room queues PostgreSQL snapshot persistence and retries after a transient failure', async t => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'projectoc-tldraw-retry-'))
  t.after(() => fs.rmSync(dataDir, { recursive: true, force: true }))
  let attempts = 0
  const persisted = []
  const manager = new OfficialRoomManager({
    dataDir,
    loadLegacySnapshot: async () => ({ snapshot: {}, snapshot_hash: '', latest_sync_event: null }),
    persistSnapshot: async (_roomKey, snapshot, metadata) => {
      attempts += 1
      if (attempts === 2) throw new Error('postgres temporarily unavailable')
      persisted.push({ snapshot, metadata })
      return { snapshot_version: attempts }
    },
    logger: { error() {}, warn() {} },
  })
  t.after(() => manager.close())

  const state = await manager.getRoom('workspace:retry:canvas-1')
  state.storage.transaction(txn => txn.set('custom:retry', { id: 'custom:retry', typeName: 'custom', value: 'durable' }))
  state.enqueuePersistence({ diff: { puts: { 'custom:retry': { id: 'custom:retry', typeName: 'custom', value: 'durable' } }, deletes: [] }, documentClock: state.storage.getClock() })
  await new Promise(resolve => setTimeout(resolve, 350))

  assert.equal(attempts >= 2, true)
  assert.equal(persisted.at(-1).metadata.protocol, OFFICIAL_PROTOCOL)
  assert.equal(state.persistPending.length, 0)
  assert.equal(state.reconciliation, 'matched')
  assert.equal(manager.health()[0].pending_persistence, 0)
})

test('official room keeps a legacy migration available while PostgreSQL is unavailable and retries it', async t => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'projectoc-tldraw-legacy-retry-'))
  t.after(() => fs.rmSync(dataDir, { recursive: true, force: true }))

  const legacySnapshot = {
    document: {
      store: {
        'shape:legacy': {
          id: 'shape:legacy',
          typeName: 'shape',
          type: 'oc-card',
          x: 12,
          y: 18,
          props: { w: 240, h: 120, kind: 'markdown', text: 'legacy content', proposalId: '' },
        },
      },
    },
    session: {},
  }
  let attempts = 0
  const persisted = []
  const manager = new OfficialRoomManager({
    dataDir,
    loadLegacySnapshot: async () => legacySnapshot,
    persistSnapshot: async (_roomKey, snapshot, metadata) => {
      attempts += 1
      if (attempts === 1) throw new Error('postgres unavailable during legacy migration')
      persisted.push({ snapshot, metadata })
      return { snapshot_version: 2 }
    },
    logger: { error() {}, warn() {} },
  })
  t.after(() => manager.close())

  const state = await manager.getRoom('workspace:legacy:canvas-1')
  assert.equal(state.migratedFromLegacy, true)
  assert.equal(state.storage.getSnapshot().documents.some(item => item.state.id === 'shape:legacy'), true)

  // The failed migration must not prevent the room from serving the legacy
  // content. The retry timer should later persist the official snapshot.
  await new Promise(resolve => setTimeout(resolve, 850))
  assert.equal(attempts >= 2, true)
  assert.equal(persisted.length, 1)
  assert.equal(persisted[0].metadata.migrated_from_legacy, true)
  assert.equal(persisted[0].metadata.protocol, OFFICIAL_PROTOCOL)
  assert.equal(state.migratedFromLegacy, false)
  assert.equal(state.persistPending.length, 0)
  assert.equal(state.reconciliation, 'matched')
})


test('simultaneous reconnects initialize one room and one SQLite storage', async t => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'projectoc-concurrent-room-'))
  let loads = 0
  const manager = new OfficialRoomManager({ dataDir, loadLegacySnapshot: async () => { loads++; await new Promise(resolve => setTimeout(resolve, 10)); return {snapshot: {}} } })
  t.after(() => { manager.close(); fs.rmSync(dataDir, { recursive: true, force: true }) })
  const rooms = await Promise.all([manager.getRoom('w:main:c'), manager.getRoom('w:main:c'), manager.getRoom('w:main:c')])
  assert.equal(loads, 1)
  assert.equal(rooms[0], rooms[1])
  assert.equal(rooms[1], rooms[2])
})
