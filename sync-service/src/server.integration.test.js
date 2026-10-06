import test from 'node:test'
import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { WebSocket } from 'ws'
import { getTlsyncProtocolVersion } from '@tldraw/sync-core'
import { projectocSchema } from './tldraw-room.js'
import { createSyncServer } from './server.js'
const secret = 'protocol-test-secret'
function ticket(role = 'editor') {
  const encoded = Buffer.from(JSON.stringify({v: 1, sub: 'user', workspace: 'workspace', canvas: 'canvas', branch: 'main', role, client_id: 'client', exp: Math.floor(Date.now()/1000)+60})).toString('base64url')
  return `v1.${encoded}.${crypto.createHmac('sha256', secret).update(encoded).digest('base64url')}`
}
async function fixture(t, options = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'projectoc-protocol-'))
  const app = createSyncServer({ticketSecret: secret, redisUrl: '', officialDataDir: dir, ...options})
  await new Promise(resolve => app.listen(0, '127.0.0.1', resolve))
  t.after(async () => { await app.close(); fs.rmSync(dir, {recursive: true, force: true}) })
  return {app, url: `ws://127.0.0.1:${app.server.address().port}/rooms/canvas?ticket=${ticket()}`}
}
async function rejection(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url)
    ws.on('unexpected-response', (_req, res) => { res.resume(); ws.terminate(); resolve(res.statusCode) })
    ws.on('error', () => {})
    ws.on('open', () => { ws.close(); reject(new Error('unexpected connection')) })
  })
}
test('records-v1 is rejected before loading or mutating any canvas', async t => {
  const {app, url} = await fixture(t, {fetchImpl: async () => { throw new Error('legacy must not access Django') }})
  assert.equal(await rejection(`${url}&protocol=records-v1`), 410)
  assert.equal(app.officialRooms.rooms.size, 0)
  const health = await (await fetch(url.replace('ws:', 'http:').split('/rooms')[0]+'/health')).json()
  assert.deepEqual(health.capabilities, ['tldraw-sync-v2'])
})
test('schema and branch mismatches reject official clients', async t => {
  const {url} = await fixture(t)
  assert.equal(await rejection(`${url}&protocol=tldraw-sync-v2&schema=old`), 409)
  assert.equal(await rejection(`${url}&branch=other`), 403)
})
test('default protocol connects an official reader even with obsolete disable options', async t => {
  const calls = []
  const state = {room: {getNumActiveSessions: () => 0}}
  const manager = {rooms: new Map(), getRoom: async key => {calls.push(key); return state}, connect: (_state, options) => {calls.push(options.isReadonly)}, closeRoom() {}, close() {}}
  const {url} = await fixture(t, {officialRooms: manager, officialCrdtEnabled: false})
  await new Promise((resolve, reject) => {
    const ws = new WebSocket(url.replace(ticket(), ticket('reader')))
    ws.on('error', reject)
    ws.on('open', () => ws.close())
    ws.on('close', resolve)
  })
  assert.deepEqual(calls, ['workspace:main:canvas', true])
})
test('migration replays frozen operations beyond the snapshot checkpoint', async t => {
  let saved
  const fetchImpl = async (url, options = {}) => {
    if (url.includes('/sync-ops/')) return {ok: true, json: async () => ({operations: [{sequence: 1, operation: {kind: 'put', clientId: 'old', opId: 'one', clock: 1, record: {id: 'shape:old', typeName: 'shape', x: 42}}}]})}
    if (options.method === 'POST' && url.endsWith('/sync-state/')) { saved = JSON.parse(options.body); return {ok: true, json: async () => ({snapshot_version: 2})} }
    return {ok: true, json: async () => options.method === 'POST' ? {} : ({snapshot: {document: {store: {}}}, snapshot_version: 1, sync_metadata: {protocol: 'records-v1'}, operation_cursor: 1, snapshot_operation_cursor: 0})}
  }
  const {app} = await fixture(t, {fetchImpl})
  const state = await app.officialRooms.getRoom('workspace:main:canvas')
  await state.pumpPersistence(state)
  assert.equal(saved.snapshot.document.store['shape:old'].x, 42)
  assert.equal(saved.sync_metadata.protocol, 'tldraw-sync-v2')
})
test('migration fails closed when historical operations are missing', async t => {
  const {app} = await fixture(t, {fetchImpl: async url => ({ok: true, json: async () => url.includes('/sync-ops/') ? {operations: []} : {snapshot: {}, sync_metadata: {protocol: 'records-v1'}, operation_cursor: 1, snapshot_operation_cursor: 0}})})
  await assert.rejects(app.officialRooms.getRoom('workspace:main:canvas'), /incomplete/)
})


test('a real official reader socket cannot create document records', async t => {
  const {app, url} = await fixture(t, {fetchImpl: async () => ({ok: true, json: async () => ({snapshot: {}, snapshot_version: 1})})})
  await new Promise((resolve, reject) => {
    const ws = new WebSocket(url.replace(ticket(), ticket('reader')))
    const timer = setTimeout(() => {ws.terminate(); reject(new Error('reader protocol timeout'))}, 3000)
    ws.on('error', reject)
    ws.on('open', () => ws.send(JSON.stringify({type: 'connect', connectRequestId: 'connect', protocolVersion: getTlsyncProtocolVersion(), schema: projectocSchema.serialize(), lastServerClock: 0})))
    ws.on('message', raw => {
      const message = JSON.parse(String(raw))
      if (message.type === 'connect') {
        assert.equal(message.isReadonly, true)
        ws.send(JSON.stringify({type: 'push', clientClock: 1, diff: {'page:attempt': ['put', {id: 'page:attempt', typeName: 'page', name: 'Forbidden', index: 'a1', meta: {}}]}}))
        setTimeout(() => {
          try {
            const state = app.officialRooms.rooms.get('workspace:main:canvas')
            assert.equal(state.storage.getSnapshot().documents.some(item => item.state.id === 'page:attempt'), false)
            clearTimeout(timer); ws.close(); resolve()
          } catch (error) {clearTimeout(timer); ws.terminate(); reject(error)}
        }, 100)
      }
    })
  })
})

test('last client disconnect does not discard pending PostgreSQL persistence', async t => {
  let closed = 0
  const state = {room: {getNumActiveSessions: () => 0}, persistPending: [{}]}
  const manager = {rooms: new Map([['workspace:main:canvas', state]]), getRoom: async () => state, connect() {}, closeRoom() { closed++ }, close() {}}
  const {url} = await fixture(t, {officialRooms: manager})
  await new Promise((resolve, reject) => {
    const ws = new WebSocket(url)
    ws.on('error', reject)
    ws.on('open', () => ws.close())
    ws.on('close', resolve)
  })
  assert.equal(closed, 0)
})
