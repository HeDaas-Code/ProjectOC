import test from 'node:test'
import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import WebSocket from 'ws'
import { createSyncServer } from './server.js'
import { createInMemoryPubSubHub, createRedisPubSub } from './pubsub.js'

const SECRET = 'integration-secret'
const canvas = 'canvas-1'
const snapshot = {
  document: { schema: { schemaVersion: 2 }, store: {
    'shape:a': { id: 'shape:a', typeName: 'shape', x: 0, y: 0, text: '原始' },
  } },
  session: { currentPageId: 'page:page' },
}

function ticket(clientId, role, roomCanvas = canvas) {
  const payload = Buffer.from(JSON.stringify({
    v: 1, sub: clientId, workspace: 'workspace-1', canvas: roomCanvas, role,
    username: clientId, client_id: clientId, exp: Math.floor(Date.now() / 1000) + 60,
  })).toString('base64url')
  const signature = crypto.createHmac('sha256', SECRET).update(payload).digest('base64url')
  return `v1.${payload}.${signature}`
}

function attachMessageQueue(ws) {
  ws.__messages = []
  ws.__waiters = []
  ws.on('message', raw => {
    let message
    try { message = JSON.parse(raw.toString()) } catch { return }
    const waiterIndex = ws.__waiters.findIndex(waiter => waiter.predicate(message))
    if (waiterIndex >= 0) {
      const [waiter] = ws.__waiters.splice(waiterIndex, 1)
      clearTimeout(waiter.timer)
      waiter.resolve(message)
    } else {
      ws.__messages.push(message)
    }
  })
}

function waitForMessage(ws, predicate, timeout = 2_000) {
  const queuedIndex = ws.__messages.findIndex(predicate)
  if (queuedIndex >= 0) {
    const [message] = ws.__messages.splice(queuedIndex, 1)
    return Promise.resolve(message)
  }
  return new Promise((resolve, reject) => {
    const waiter = {predicate, resolve, timer: undefined}
    waiter.timer = setTimeout(() => {
      const index = ws.__waiters.indexOf(waiter)
      if (index >= 0) ws.__waiters.splice(index, 1)
      reject(new Error('timed out waiting for WebSocket message'))
    }, timeout)
    ws.__waiters.push(waiter)
  })
}

async function openClient(port, clientId, role, after = null, roomCanvas = canvas) {
  const replay = after === null ? '' : `&after=${encodeURIComponent(after)}`
  const ws = new WebSocket(`ws://127.0.0.1:${port}/rooms/${roomCanvas}?ticket=${ticket(clientId, role, roomCanvas)}${replay}`)
  attachMessageQueue(ws)
  const hello = waitForMessage(ws, message => message.type === 'hello')
  await new Promise((resolve, reject) => {
    ws.once('open', resolve)
    ws.once('error', reject)
  })
  ws.__hello = await hello
  return ws
}

function operation(clientId, clock, opId, record) {
  return {kind: 'put', clientId, clock, opId, record}
}

test('two editor clients merge record operations and persist materialized state', async t => {
  let version = 1
  const posts = []
  const app = createSyncServer({
    ticketSecret: SECRET,
    fetchImpl: async (_url, request = {}) => {
      if (request.method === 'POST') {
        const body = JSON.parse(request.body)
        posts.push(body)
        version += 1
        return {ok: true, status: 200, json: async () => ({snapshot_version: version})}
      }
      return {ok: true, status: 200, json: async () => ({snapshot, snapshot_version: version, sync_metadata: {}})}
    },
  })
  await new Promise(resolve => app.listen(0, '127.0.0.1', resolve))
  t.after(async () => { await app.close() })
  const port = app.server.address().port
  const editorA = await openClient(port, 'editor-a', 'editor')
  const editorB = await openClient(port, 'editor-b', 'editor')
  t.after(() => { editorA.close(); editorB.close() })

  const persistedPromise = waitForMessage(editorA, message => message.type === 'persisted' && message.version >= 3)
  const a = operation('editor-a', 1, 'a-1', {id: 'shape:a', typeName: 'shape', x: 10})
  const b = operation('editor-b', 1, 'b-1', {id: 'shape:a', typeName: 'shape', text: '并发文本'})
  const aAckPromise = waitForMessage(editorA, message => message.type === 'ops_ack' && message.accepted.includes('editor-a:a-1'))
  editorA.send(JSON.stringify({type: 'ops', ops: [a]}))
  const aAck = await aAckPromise
  assert.deepEqual(aAck.rejected, [])
  const aForB = await waitForMessage(editorB, message => message.type === 'ops' && message.ops.some(op => op.opId === 'a-1'))
  assert.equal(aForB.ops[0].record.x, 10)

  const bAckPromise = waitForMessage(editorB, message => message.type === 'ops_ack' && message.accepted.includes('editor-b:b-1'))
  editorB.send(JSON.stringify({type: 'ops', ops: [b]}))
  const bAck = await bAckPromise
  assert.deepEqual(bAck.rejected, [])
  const bForA = await waitForMessage(editorA, message => message.type === 'ops' && message.ops.some(op => op.opId === 'b-1'))
  assert.equal(bForA.ops[0].record.text, '并发文本')

  await persistedPromise
  assert.ok(posts.length >= 2)
  const latest = posts.at(-1)
  assert.equal(latest.snapshot.document.store['shape:a'].x, 10)
  assert.equal(latest.snapshot.document.store['shape:a'].text, '并发文本')
})

test('successful snapshot checkpoints trigger safe operation-log compaction', async t => {
  let version = 1
  let cursor = 0
  const operations = []
  const compactions = []
  const app = createSyncServer({
    ticketSecret: SECRET,
    operationCompactionThreshold: 1,
    operationCompactionKeepLast: 0,
    fetchImpl: async (url, request = {}) => {
      const parsed = new URL(url)
      if (parsed.pathname.endsWith('/sync-state/')) {
        if (request.method === 'POST') {
          const body = JSON.parse(request.body)
          assert.equal(body.operation_cursor, 1)
          version += 1
          return {ok: true, status: 200, json: async () => ({snapshot_version: version, operation_cursor: cursor, operation_compacted_through: 0})}
        }
        return {ok: true, status: 200, json: async () => ({snapshot, snapshot_version: version, operation_cursor: cursor, operation_compacted_through: 0, sync_metadata: {}})}
      }
      if (parsed.pathname.endsWith('/sync-ops/')) {
        if (request.method === 'GET') {
          const after = Number(parsed.searchParams.get('after') || 0)
          return {ok: true, status: 200, json: async () => ({operations: operations.filter(item => item.sequence > after), cursor, has_more: false})}
        }
        const body = JSON.parse(request.body)
        const rows = body.operations.map(item => ({sequence: ++cursor, operation: item}))
        operations.push(...rows)
        return {ok: true, status: 200, json: async () => ({operations: rows, cursor})}
      }
      if (parsed.pathname.endsWith('/compact-ops/')) {
        const body = JSON.parse(request.body)
        compactions.push(body)
        return {ok: true, status: 200, json: async () => ({compacted_through: body.through, deleted: 1})}
      }
      throw new Error(`unexpected fake fetch URL ${url}`)
    },
  })
  await new Promise(resolve => app.listen(0, '127.0.0.1', resolve))
  t.after(async () => { await app.close() })
  const port = app.server.address().port
  const editor = await openClient(port, 'compaction-editor', 'editor')
  t.after(() => editor.close())

  const persisted = waitForMessage(editor, message => message.type === 'persisted' && message.version >= 2)
  const ack = waitForMessage(editor, message => message.type === 'ops_ack' && message.accepted.includes('compaction-editor:compact-1'))
  editor.send(JSON.stringify({type: 'ops', ops: [operation('compaction-editor', 1, 'compact-1', {id: 'shape:compact', typeName: 'shape', text: 'checkpoint'})]}))
  await Promise.all([ack, persisted])
  for (let attempt = 0; attempt < 20 && !compactions.length; attempt += 1) await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(compactions.length, 1)
  assert.deepEqual(compactions[0], {through: 1, keep_last: 0})
})


test('snapshot conflicts retain live operation state instead of broadcasting a stale rollback', async t => {
  let version = 1
  let cursor = 0
  let persistedSnapshot = structuredClone(snapshot)
  let conflict = true
  const snapshotPosts = []
  const operations = []
  const app = createSyncServer({
    ticketSecret: SECRET,
    fetchImpl: async (url, request = {}) => {
      const parsed = new URL(url)
      if (parsed.pathname.endsWith('/sync-state/')) {
        if (request.method === 'POST') {
          const body = JSON.parse(request.body)
          snapshotPosts.push(body)
          if (conflict) {
            conflict = false
            return {
              ok: false,
              status: 409,
              json: async () => ({
                detail: 'stale snapshot',
                snapshot: structuredClone(snapshot),
                snapshot_version: version,
                operation_cursor: cursor,
                sync_metadata: {},
              }),
            }
          }
          persistedSnapshot = body.snapshot
          version += 1
          return {ok: true, status: 200, json: async () => ({snapshot_version: version, operation_cursor: cursor})}
        }
        return {ok: true, status: 200, json: async () => ({snapshot: persistedSnapshot, snapshot_version: version, operation_cursor: cursor, sync_metadata: {}})}
      }
      if (parsed.pathname.endsWith('/sync-ops/')) {
        if (request.method === 'GET') {
          const after = Number(parsed.searchParams.get('after') || 0)
          const rows = operations.filter(item => item.sequence > after)
          return {ok: true, status: 200, json: async () => ({operations: rows, cursor, has_more: false})}
        }
        const body = JSON.parse(request.body)
        const rows = body.operations.map(operation => {
          const existing = operations.find(item => item.operation.opId === operation.opId)
          if (existing) return existing
          const row = {sequence: ++cursor, operation}
          operations.push(row)
          return row
        })
        return {ok: true, status: 200, json: async () => ({operations: rows, cursor})}
      }
      throw new Error(`unexpected fake fetch URL ${url}`)
    },
  })
  await new Promise(resolve => app.listen(0, '127.0.0.1', resolve))
  t.after(async () => { await app.close() })
  const port = app.server.address().port
  const editor = await openClient(port, 'conflict-editor', 'editor')
  const reader = await openClient(port, 'conflict-reader', 'reader')
  t.after(() => { editor.close(); reader.close() })

  const operationSeen = waitForMessage(reader, message => message.type === 'ops' && message.ops?.some(op => op.opId === 'conflict-1'))
  const persisted = waitForMessage(editor, message => message.type === 'persisted' && message.version >= 2)
  const ack = waitForMessage(editor, message => message.type === 'ops_ack' && message.accepted.includes('conflict-editor:conflict-1') && message.durable === true)
  editor.send(JSON.stringify({type: 'ops', ops: [operation('conflict-editor', 1, 'conflict-1', {id: 'shape:conflict', typeName: 'shape', text: '实时状态'})]}))

  await Promise.all([operationSeen, ack, persisted])
  await new Promise(resolve => setTimeout(resolve, 100))
  assert.equal(reader.__messages.some(message => message.type === 'snapshot' && !message.snapshot?.document?.store?.['shape:conflict']), false)
  assert.equal(persistedSnapshot.document.store['shape:conflict'].text, '实时状态')
  assert.equal(app.rooms.get(canvas).snapshot.document.store['shape:conflict'].text, '实时状态')
  assert.equal(snapshotPosts.length >= 2, true)
})

test('an editor establishes the full TLDraw baseline before record operations', async t => {
  const posts = []
  let version = 1
  const emptySnapshot = {document: {schema: {schemaVersion: 2}, store: {}}, session: {currentPageId: 'page:page'}}
  const app = createSyncServer({
    ticketSecret: SECRET,
    fetchImpl: async (_url, request = {}) => {
      if (request.method === 'POST') {
        const body = JSON.parse(request.body)
        posts.push(body)
        version += 1
        return {ok: true, status: 200, json: async () => ({snapshot_version: version, operation_cursor: 0})}
      }
      return {ok: true, status: 200, json: async () => ({snapshot: emptySnapshot, snapshot_version: version, sync_metadata: {}})}
    },
  })
  await new Promise(resolve => app.listen(0, '127.0.0.1', resolve))
  t.after(async () => { await app.close() })
  const port = app.server.address().port
  const editor = await openClient(port, 'baseline-editor', 'editor', null, 'baseline-canvas')
  t.after(() => editor.close())

  assert.equal(editor.__hello.needs_baseline, true)
  const fullSnapshot = {
    document: {
      schema: {schemaVersion: 2},
      store: {
        'document:document': {id: 'document:document', typeName: 'document', meta: {}},
        'page:page': {id: 'page:page', typeName: 'page', name: 'Page 1', index: 'a1'},
        'shape:root': {id: 'shape:root', typeName: 'shape', x: 10, y: 20, props: {text: '基线'}},
      },
    },
    session: {currentPageId: 'page:page'},
  }
  const baselineAck = waitForMessage(editor, message => message.type === 'baseline_ack')
  editor.send(JSON.stringify({type: 'baseline', snapshot: fullSnapshot, sync_metadata: {protocol: 'records-v1'}}))
  assert.deepEqual(await baselineAck, {type: 'baseline_ack', accepted: true, version: 1, operation_cursor: 0, state_vector: {}})

  const persisted = waitForMessage(editor, message => message.type === 'persisted' && message.version >= 2)
  const ack = waitForMessage(editor, message => message.type === 'ops_ack' && message.accepted.includes('baseline-editor:baseline-op'))
  editor.send(JSON.stringify({type: 'ops', ops: [operation('baseline-editor', 1, 'baseline-op', {id: 'shape:root', typeName: 'shape', x: 30})]}))
  assert.deepEqual((await ack).rejected, [])
  await persisted

  assert.equal(posts.length >= 2, true)
  const baselinePost = posts.find(post => post.snapshot?.document?.store?.['shape:root']?.x === 10)
  assert.ok(baselinePost)
  assert.equal(baselinePost.sync_metadata?.baseline_ready, true)
  assert.ok(baselinePost.snapshot.document.store['page:page'])
  assert.equal(baselinePost.snapshot.document.store['shape:root'].x, 10)

  // A later connection must receive the materialized full document, not the
  // empty bootstrap snapshot, and must not be asked to establish baseline again.
  const reconnect = await openClient(port, 'baseline-reader', 'reader', null, 'baseline-canvas')
  t.after(() => reconnect.close())
  assert.equal(reconnect.__hello.needs_baseline, false)
  assert.equal(reconnect.__hello.snapshot.document.store['page:page'].name, 'Page 1')
  assert.equal(reconnect.__hello.snapshot.document.store['shape:root'].x, 30)
})

test('readers cannot initialize a missing TLDraw baseline', async t => {
  const app = createSyncServer({
    ticketSecret: SECRET,
    fetchImpl: async () => ({ok: true, status: 200, json: async () => ({snapshot: {document: {store: {}}, session: {}}, snapshot_version: 1, sync_metadata: {}})}),
  })
  await new Promise(resolve => app.listen(0, '127.0.0.1', resolve))
  t.after(async () => { await app.close() })
  const port = app.server.address().port
  const reader = await openClient(port, 'baseline-reader-only', 'reader', null, 'reader-baseline-canvas')
  t.after(() => reader.close())

  assert.equal(reader.__hello.needs_baseline, true)
  const error = waitForMessage(reader, message => message.type === 'error')
  reader.send(JSON.stringify({type: 'baseline', snapshot: {document: {store: {'page:page': {id: 'page:page'}}}}}))
  assert.deepEqual(await error, {type: 'error', code: 'read_only', detail: 'reader cannot initialize a canvas'})
})

test('reader is read-only and duplicate/stale operations are safe', async t => {
  let version = 1
  const app = createSyncServer({
    ticketSecret: SECRET,
    fetchImpl: async (_url, request = {}) => {
      if (request.method === 'POST') {
        version += 1
        return {ok: true, status: 200, json: async () => ({snapshot_version: version})}
      }
      return {ok: true, status: 200, json: async () => ({snapshot, snapshot_version: version, sync_metadata: {}})}
    },
  })
  await new Promise(resolve => app.listen(0, '127.0.0.1', resolve))
  t.after(async () => { await app.close() })
  const port = app.server.address().port
  const editor = await openClient(port, 'editor-c', 'editor')
  const reader = await openClient(port, 'reader-c', 'reader')
  t.after(() => { editor.close(); reader.close() })

  const first = operation('editor-c', 1, 'c-1', {id: 'shape:b', typeName: 'shape', x: 2})
  const firstAckPromise = waitForMessage(editor, message => message.type === 'ops_ack' && message.accepted.includes('editor-c:c-1'))
  editor.send(JSON.stringify({type: 'ops', ops: [first]}))
  const firstAck = await firstAckPromise
  assert.deepEqual(firstAck.rejected, [])

  const duplicateAckPromise = waitForMessage(editor, message => message.type === 'ops_ack' && message.accepted.length === 0)
  editor.send(JSON.stringify({type: 'ops', ops: [first]}))
  const duplicateAck = await duplicateAckPromise
  assert.equal(duplicateAck.rejected[0].reason, 'duplicate')

  const remove = {kind: 'remove', clientId: 'editor-c', clock: 3, opId: 'c-remove', recordId: 'shape:b'}
  const removeAckPromise = waitForMessage(editor, message => message.type === 'ops_ack' && message.accepted.includes('editor-c:c-remove'))
  editor.send(JSON.stringify({type: 'ops', ops: [remove]}))
  await removeAckPromise
  const stale = operation('editor-c', 2, 'c-stale', {id: 'shape:b', typeName: 'shape', x: 99})
  const staleAckPromise = waitForMessage(editor, message => message.type === 'ops_ack' && message.accepted.length === 0 && message.rejected.length === 1)
  editor.send(JSON.stringify({type: 'ops', ops: [stale]}))
  const staleAck = await staleAckPromise
  assert.equal(staleAck.rejected[0].reason, 'stale_operation')

  const readerErrorPromise = waitForMessage(reader, message => message.type === 'error' && message.code === 'read_only')
  reader.send(JSON.stringify({type: 'ops', ops: [operation('reader-c', 1, 'reader-1', {id: 'shape:c', typeName: 'shape'})]}))
  const readerError = await readerErrorPromise
  assert.equal(readerError.code, 'read_only')
})



test('sync service restart reconstructs live canvas state from the durable operation log', async t => {
  let cursor = 0
  const operations = []
  const persistedSnapshot = structuredClone(snapshot)
  const fetchImpl = async (url, request = {}) => {
    const parsed = new URL(url)
    if (parsed.pathname.endsWith('/sync-state/')) {
      if (request.method === 'POST') {
        return {ok: true, status: 200, json: async () => ({snapshot_version: 2, operation_cursor: cursor})}
      }
      return {ok: true, status: 200, json: async () => ({
        snapshot: structuredClone(persistedSnapshot),
        snapshot_version: 1,
        operation_cursor: 0,
        sync_metadata: {},
      })}
    }
    if (parsed.pathname.endsWith('/sync-ops/')) {
      if (request.method === 'GET') {
        const after = Number(parsed.searchParams.get('after') || 0)
        const rows = operations.filter(item => item.sequence > after)
        return {ok: true, status: 200, json: async () => ({operations: rows, cursor, has_more: false})}
      }
      const body = JSON.parse(request.body)
      const rows = body.operations.map(operation => {
        const existing = operations.find(item => item.operation.opId === operation.opId)
        if (existing) return existing
        const row = {sequence: ++cursor, operation}
        operations.push(row)
        return row
      })
      return {ok: true, status: 200, json: async () => ({operations: rows, cursor})}
    }
    throw new Error(`unexpected fake fetch URL ${url}`)
  }

  const first = createSyncServer({ticketSecret: SECRET, fetchImpl})
  await new Promise(resolve => first.listen(0, '127.0.0.1', resolve))
  const firstPort = first.server.address().port
  const editor = await openClient(firstPort, 'restart-editor', 'editor')
  const ack = waitForMessage(editor, message => message.type === 'ops_ack' && message.accepted.includes('restart-editor:restart-1') && message.durable === true)
  editor.send(JSON.stringify({type: 'ops', ops: [operation('restart-editor', 1, 'restart-1', {id: 'shape:restart', typeName: 'shape', text: '重启前已落盘到操作日志'})]}))
  await ack
  await new Promise(resolve => setTimeout(resolve, 75))
  editor.close()
  await first.close()

  assert.equal(cursor, 1)
  assert.equal(operations[0].operation.opId, 'restart-1')

  const second = createSyncServer({ticketSecret: SECRET, fetchImpl})
  await new Promise(resolve => second.listen(0, '127.0.0.1', resolve))
  t.after(async () => { await second.close() })
  const reader = await openClient(second.server.address().port, 'restart-reader', 'reader')
  t.after(() => reader.close())

  assert.equal(reader.__hello.operation_cursor, 1)
  assert.equal(reader.__hello.snapshot.document.store['shape:restart'].text, '重启前已落盘到操作日志')
  assert.equal(second.rooms.get(canvas).snapshot.document.store['shape:restart'].text, '重启前已落盘到操作日志')
})

test('durable operation log gates broadcast, retries failed appends, and replays after reconnect', async t => {
  let version = 1
  let cursor = 0
  let snapshotState = structuredClone(snapshot)
  let operations = []
  let failures = 1
  const posts = []
  const app = createSyncServer({
    ticketSecret: SECRET,
    fetchImpl: async (url, request = {}) => {
      const parsed = new URL(url)
      if (parsed.pathname.endsWith('/sync-state/')) {
        if (request.method === 'POST') {
          const body = JSON.parse(request.body)
          posts.push(body)
          snapshotState = body.snapshot
          version += 1
          return {ok: true, status: 200, json: async () => ({snapshot_version: version, operation_cursor: cursor})}
        }
        return {ok: true, status: 200, json: async () => ({snapshot: snapshotState, snapshot_version: version, operation_cursor: cursor, sync_metadata: {}})}
      }
      if (parsed.pathname.endsWith('/sync-ops/')) {
        if (request.method === 'GET') {
          const after = Number(parsed.searchParams.get('after') || 0)
          const rows = operations.filter(item => item.sequence > after).slice(0, 200)
          return {ok: true, status: 200, json: async () => ({operations: rows, cursor, has_more: rows.length > 0 && rows.at(-1).sequence < cursor})}
        }
        if (failures > 0) {
          failures -= 1
          return {ok: false, status: 503, json: async () => ({detail: 'temporary durable log outage'})}
        }
        const body = JSON.parse(request.body)
        const rows = []
        for (const operation of body.operations) {
          const existing = operations.find(item => item.operation.opId === operation.opId)
          if (existing) rows.push(existing)
          else {
            cursor += 1
            const row = {sequence: cursor, operation}
            operations.push(row)
            rows.push(row)
          }
        }
        return {ok: true, status: 200, json: async () => ({operations: rows, cursor})}
      }
      throw new Error(`unexpected fake fetch URL ${url}`)
    },
  })
  await new Promise(resolve => app.listen(0, '127.0.0.1', resolve))
  t.after(async () => { await app.close() })
  const port = app.server.address().port
  const editor = await openClient(port, 'durable-editor', 'editor')
  const reader = await openClient(port, 'durable-reader', 'reader')
  t.after(() => { editor.close(); reader.close() })

  const first = operation('durable-editor', 1, 'durable-1', {id: 'shape:durable', typeName: 'shape', text: '必须落盘'})
  const failedAck = waitForMessage(editor, message => message.type === 'ops_ack' && message.accepted.includes('durable-editor:durable-1') && message.durable === false)
  editor.send(JSON.stringify({type: 'ops', ops: [first]}))
  await failedAck
  await new Promise(resolve => setTimeout(resolve, 100))
  assert.equal(reader.__messages.some(message => message.type === 'ops' && message.ops?.some(op => op.opId === 'durable-1')), false)

  const readerOp = waitForMessage(reader, message => message.type === 'ops' && message.ops?.some(op => op.opId === 'durable-1'))
  const durableAck = waitForMessage(editor, message => message.type === 'ops_ack' && message.accepted.includes('durable-editor:durable-1') && message.durable === true)
  const persisted = waitForMessage(editor, message => message.type === 'persisted' && message.operation_cursor === 1)
  await Promise.all([readerOp, durableAck, persisted])
  assert.equal(cursor, 1)
  assert.equal(posts.at(-1).snapshot.document.store['shape:durable'].text, '必须落盘')

  editor.close()
  reader.close()
  await new Promise(resolve => setTimeout(resolve, 50))
  const replayClient = await openClient(port, 'replay-reader', 'reader', 0)
  t.after(() => replayClient.close())
  const hello = replayClient.__hello
  assert.equal(hello.snapshot, null)
  const replay = await waitForMessage(replayClient, message => message.type === 'ops_replay' && message.ops.some(op => op.opId === 'durable-1'))
  assert.equal(replay.operation_cursor, 1)
})

test('two sync instances coordinate operations and presence without duplicating durable log entries', async t => {
  let snapshotState = structuredClone(snapshot)
  let snapshotVersion = 1
  let cursor = 0
  const operations = []
  const appendCalls = []
  const fetchImpl = async (url, request = {}) => {
    const parsed = new URL(url)
    if (parsed.pathname.endsWith('/sync-state/')) {
      if (request.method === 'POST') {
        snapshotVersion += 1
        return {ok: true, status: 200, json: async () => ({snapshot_version: snapshotVersion, operation_cursor: cursor})}
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({snapshot: structuredClone(snapshotState), snapshot_version: snapshotVersion, operation_cursor: 0, sync_metadata: {}}),
      }
    }
    if (parsed.pathname.endsWith('/sync-ops/')) {
      if (request.method === 'GET') {
        const after = Number(parsed.searchParams.get('after') || 0)
        const rows = operations.filter(item => item.sequence > after)
        return {ok: true, status: 200, json: async () => ({operations: rows, cursor, has_more: false})}
      }
      const body = JSON.parse(request.body)
      appendCalls.push(body)
      const rows = []
      for (const operation of body.operations || []) {
        const existing = operations.find(item => item.operation.opId === operation.opId)
        if (existing) rows.push(existing)
        else {
          cursor += 1
          const row = {sequence: cursor, operation}
          operations.push(row)
          rows.push(row)
        }
      }
      return {ok: true, status: 200, json: async () => ({operations: rows, cursor})}
    }
    throw new Error(`unexpected fake fetch URL ${url}`)
  }

  const hub = createInMemoryPubSubHub()
  const appA = createSyncServer({
    ticketSecret: SECRET,
    fetchImpl,
    pubsub: hub.createClient('instance-a'),
  })
  const appB = createSyncServer({
    ticketSecret: SECRET,
    fetchImpl,
    pubsub: hub.createClient('instance-b'),
  })
  let appAClosed = false
  t.after(async () => {
    if (!appAClosed) await appA.close()
    await appB.close()
  })
  await Promise.all([
    new Promise(resolve => appA.listen(0, '127.0.0.1', resolve)),
    new Promise(resolve => appB.listen(0, '127.0.0.1', resolve)),
  ])

  const portA = appA.server.address().port
  const portB = appB.server.address().port
  const editorA = await openClient(portA, 'multi-editor-a', 'editor')
  const editorB = await openClient(portB, 'multi-editor-b', 'editor')
  t.after(() => { editorA.close(); editorB.close() })

  const joinedOnA = waitForMessage(editorA, message => message.type === 'presence' && message.event === 'joined' && message.collaborator?.id === 'multi-editor-b')
  editorB.send(JSON.stringify({type: 'presence', state: {cursor: {x: 12, y: 34}, selection: ['shape:a']}}))
  const presenceOnA = waitForMessage(editorA, message => message.type === 'presence' && message.event === 'update' && message.collaborator?.id === 'multi-editor-b')
  await joinedOnA
  const remotePresence = await presenceOnA
  assert.deepEqual(remotePresence.collaborator.cursor, {x: 12, y: 34})
  assert.deepEqual(remotePresence.collaborator.selection, ['shape:a'])

  const first = operation('multi-editor-a', 1, 'multi-a-1', {id: 'shape:multi', typeName: 'shape', text: '来自实例 A'})
  const operationOnB = waitForMessage(editorB, message => message.type === 'ops' && message.ops?.some(op => op.opId === 'multi-a-1'))
  const ackOnA = waitForMessage(editorA, message => message.type === 'ops_ack' && message.accepted.includes('multi-editor-a:multi-a-1') && message.durable === true)
  editorA.send(JSON.stringify({type: 'ops', ops: [first]}))
  await Promise.all([operationOnB, ackOnA])
  assert.equal(appendCalls.length, 1)
  assert.equal(operations.length, 1)
  assert.equal(appB.rooms.get(canvas).operationCursor, 1)

  // A duplicate pub/sub event must not create a second broadcast or log row.
  const duplicateEvent = {
    version: 1,
    id: 'external-duplicate-event',
    instance_id: 'external-test-publisher',
    canvas,
    type: 'ops',
    payload: {ops: [first], operation_cursor: 1, version: 1, source: 'multi-editor-a'},
  }
  const external = hub.createClient('external-test-publisher')
  await external.publish(`projectoc:sync:room:${canvas}`, duplicateEvent)
  await external.publish(`projectoc:sync:room:${canvas}`, duplicateEvent)
  await new Promise(resolve => setTimeout(resolve, 50))
  assert.equal(editorB.__messages.some(message => message.type === 'ops' && message.ops?.some(op => op.opId === 'multi-a-1')), false)
  assert.equal(appendCalls.length, 1)
  assert.equal(operations.length, 1)

  // The surviving instance can continue writing after instance A disappears.
  await appA.close()
  appAClosed = true
  editorA.close()
  const second = operation('multi-editor-b', 1, 'multi-b-1', {id: 'shape:multi-b', typeName: 'shape', text: '来自实例 B'})
  const ackOnB = waitForMessage(editorB, message => message.type === 'ops_ack' && message.accepted.includes('multi-editor-b:multi-b-1') && message.durable === true)
  editorB.send(JSON.stringify({type: 'ops', ops: [second]}))
  await ackOnB
  assert.equal(appendCalls.length, 2)
  assert.equal(operations.length, 2)

  // Recreate the room on instance B and replay the durable log from cursor 0.
  editorB.close()
  await new Promise(resolve => setTimeout(resolve, 50))
  const replayClient = await openClient(portB, 'multi-replay-reader', 'reader', 0)
  t.after(() => replayClient.close())
  const replay = await waitForMessage(replayClient, message => message.type === 'ops_replay' && message.ops?.some(op => op.opId === 'multi-b-1'))
  assert.deepEqual(replay.ops.map(op => op.opId), ['multi-a-1', 'multi-b-1'])
  assert.equal(replay.operation_cursor, 2)
  assert.equal(appB.rooms.get(canvas).operationCursor, 2)
  assert.equal(appendCalls.length, 2)
})

test('real Redis pub/sub coordinates two sync instances', {skip: !process.env.RUN_REDIS_INTEGRATION}, async t => {
  const redisCanvas = `redis-${crypto.randomUUID()}`
  const prefix = `projectoc:test:${process.pid}:${Date.now()}`
  let cursor = 0
  let version = 1
  const operations = []
  let snapshotState = structuredClone(snapshot)
  const appendCalls = []
  const fetchImpl = async (url, request = {}) => {
    const parsed = new URL(url)
    if (parsed.pathname.endsWith('/sync-state/')) {
      if (request.method === 'POST') {
        version += 1
        return {ok: true, status: 200, json: async () => ({snapshot_version: version, operation_cursor: cursor})}
      }
      return {ok: true, status: 200, json: async () => ({snapshot: structuredClone(snapshotState), snapshot_version: version, operation_cursor: 0, sync_metadata: {}})}
    }
    if (parsed.pathname.endsWith('/sync-ops/')) {
      if (request.method === 'GET') {
        const after = Number(parsed.searchParams.get('after') || 0)
        const rows = operations.filter(item => item.sequence > after)
        return {ok: true, status: 200, json: async () => ({operations: rows, cursor, has_more: false})}
      }
      const body = JSON.parse(request.body)
      appendCalls.push(body)
      const rows = []
      for (const operation of body.operations || []) {
        const existing = operations.find(item => item.operation.opId === operation.opId)
        if (existing) rows.push(existing)
        else {
          cursor += 1
          const row = {sequence: cursor, operation}
          operations.push(row)
          rows.push(row)
        }
      }
      return {ok: true, status: 200, json: async () => ({operations: rows, cursor})}
    }
    throw new Error(`unexpected fake fetch URL ${url}`)
  }

  const redisUrl = process.env.SYNC_REDIS_URL || process.env.REDIS_URL || 'redis://127.0.0.1:6379'
  const pubsubA = createRedisPubSub({url: redisUrl, prefix, instanceId: 'redis-instance-a'})
  const pubsubB = createRedisPubSub({url: redisUrl, prefix, instanceId: 'redis-instance-b'})
  const appA = createSyncServer({ticketSecret: SECRET, fetchImpl, pubsub: pubsubA})
  const appB = createSyncServer({ticketSecret: SECRET, fetchImpl, pubsub: pubsubB})
  t.after(async () => { await appA.close(); await appB.close() })
  await Promise.all([
    new Promise(resolve => appA.listen(0, '127.0.0.1', resolve)),
    new Promise(resolve => appB.listen(0, '127.0.0.1', resolve)),
  ])

  const editorA = await openClient(appA.server.address().port, 'redis-editor-a', 'editor', null, redisCanvas)
  const editorB = await openClient(appB.server.address().port, 'redis-editor-b', 'editor', null, redisCanvas)
  t.after(() => { editorA.close(); editorB.close() })

  const presenceOnA = waitForMessage(editorA, message => message.type === 'presence' && message.event === 'update' && message.collaborator?.id === 'redis-editor-b')
  editorB.send(JSON.stringify({type: 'presence', state: {cursor: {x: 7, y: 8}}}))
  assert.deepEqual((await presenceOnA).collaborator.cursor, {x: 7, y: 8})

  const op = operation('redis-editor-a', 1, 'redis-op-1', {id: 'shape:redis', typeName: 'shape', text: 'Redis 多实例'})
  const remoteOp = waitForMessage(editorB, message => message.type === 'ops' && message.ops?.some(item => item.opId === 'redis-op-1'))
  const ack = waitForMessage(editorA, message => message.type === 'ops_ack' && message.accepted.includes('redis-editor-a:redis-op-1') && message.durable === true)
  editorA.send(JSON.stringify({type: 'ops', ops: [op]}))
  await Promise.all([remoteOp, ack])
  assert.equal(appendCalls.length, 1)
  assert.equal(operations.length, 1)
  assert.equal(pubsubA.health.status, 'ready')
  assert.equal(pubsubB.health.status, 'ready')
})
