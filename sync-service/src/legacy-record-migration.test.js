import test from 'node:test'
import assert from 'node:assert/strict'
import { applyOperation, applyOperations, compareVersion, createRecordState, metadataFromRecordState, shapeFieldCategory, shapeMergeStrategy, snapshotFromRecordState } from './legacy-record-migration.js'

const snapshot = {
  document: { schema: { schemaVersion: 2 }, store: {
    'shape:a': { id: 'shape:a', typeName: 'shape', x: 0, y: 0, text: '原始' },
  } },
  session: { currentPageId: 'page:page' },
}
const op = (clientId, clock, record, opId = `${clientId}-${clock}`) => ({ kind: 'put', clientId, clock, opId, record })

 test('record operations merge independent concurrent fields deterministically', () => {
  const left = createRecordState(snapshot)
  const right = createRecordState(snapshot)
  const a = op('a', 1, { id: 'shape:a', typeName: 'shape', x: 10 })
  const b = op('b', 1, { id: 'shape:a', typeName: 'shape', text: '并发' })
  applyOperation(left, a); applyOperation(left, b)
  applyOperation(right, b); applyOperation(right, a)
  assert.deepEqual(left.records.get('shape:a'), right.records.get('shape:a'))
  assert.equal(left.records.get('shape:a').x, 10)
  assert.equal(left.records.get('shape:a').text, '并发')
})

test('remove is a tombstone and an older update cannot resurrect a record', () => {
  const state = createRecordState(snapshot)
  applyOperation(state, { kind: 'remove', clientId: 'z', clock: 3, opId: 'remove-3', recordId: 'shape:a' })
  const stale = applyOperation(state, op('a', 2, { id: 'shape:a', typeName: 'shape', x: 9 }))
  assert.equal(stale.accepted, false)
  assert.equal(state.records.has('shape:a'), false)
  const newer = applyOperation(state, op('a', 4, { id: 'shape:a', typeName: 'shape', x: 12 }))
  assert.equal(newer.accepted, true)
  assert.equal(state.records.get('shape:a').x, 12)
})

test('duplicate operation is idempotent', () => {
  const state = createRecordState(snapshot)
  const operation = op('a', 1, { id: 'shape:b', typeName: 'shape', x: 3 })
  assert.equal(applyOperation(state, operation).accepted, true)
  const duplicate = applyOperation(state, operation)
  assert.equal(duplicate.duplicate, true)
  assert.equal(state.records.size, 2)
})

test('snapshot materialization preserves session and schema', () => {
  const state = createRecordState(snapshot)
  applyOperation(state, op('a', 1, { id: 'shape:b', typeName: 'shape', x: 2 }))
  const next = snapshotFromRecordState(snapshot, state)
  assert.equal(next.session.currentPageId, 'page:page')
  assert.ok(next.document.schema)
  assert.ok(next.document.store['shape:b'])
})

test('batch caps input and rejects malformed operations', () => {
  const state = createRecordState(snapshot)
  const result = applyOperations(state, [{ kind: 'wat', clientId: 'a', clock: 1, opId: 'x' }])
  assert.equal(result.accepted.length, 0)
  assert.equal(result.rejected[0].reason, 'invalid_operation')
  assert.equal(compareVersion({ clock: 2, clientId: 'a', opId: 'x' }, { clock: 1, clientId: 'z', opId: 'z' }) > 0, true)
})

test('field versions and deletion tombstones survive state serialization', () => {
  const state = createRecordState(snapshot)
  applyOperation(state, op('a', 4, { id: 'shape:a', typeName: 'shape', text: '新文本' }))
  applyOperation(state, { kind: 'remove', clientId: 'a', clock: 5, opId: 'a-5', recordId: 'shape:a' })
  const metadata = metadataFromRecordState(state)
  const restored = createRecordState(snapshotFromRecordState(snapshot, state), metadata)
  assert.equal(applyOperation(restored, op('a', 3, { id: 'shape:a', typeName: 'shape', text: '旧文本' })).accepted, false)
  assert.equal(restored.records.has('shape:a'), false)
  assert.equal(applyOperation(restored, op('a', 6, { id: 'shape:a', typeName: 'shape', text: '恢复' })).accepted, true)
})

test('partial LWW loss reports the exact fields and winning values', () => {
  const state = createRecordState(snapshot)
  const winner = op('winner', 5, { id: 'shape:a', typeName: 'shape', x: 42, text: '远端胜出' })
  const local = op('local', 4, { id: 'shape:a', typeName: 'shape', x: 9, y: 11, text: '本地旧值' })
  assert.equal(applyOperation(state, winner).accepted, true)
  const result = applyOperation(state, local)
  assert.equal(result.accepted, true)
  assert.deepEqual(result.conflicted_fields, ['x', 'text'])
  assert.deepEqual(result.conflict.current, { id: 'shape:a', typeName: 'shape', x: 42, y: 11, text: '远端胜出' })
  assert.equal(result.conflict.field_versions.x.clientId, 'winner')
  assert.equal(result.conflict.field_versions.text.clientId, 'winner')
})

test('stale remove reports the surviving record for user-visible conflict resolution', () => {
  const state = createRecordState(snapshot)
  applyOperation(state, op('winner', 5, { id: 'shape:a', typeName: 'shape', text: '保留' }))
  const result = applyOperation(state, { kind: 'remove', clientId: 'local', clock: 4, opId: 'local-remove', recordId: 'shape:a' })
  assert.equal(result.accepted, false)
  assert.deepEqual(result.conflicted_fields, ['x', 'y', 'text'])
  assert.equal(result.conflict.current.text, '保留')
})


test('causal dependencies are deferred until their prerequisites are applied', () => {
  const state = createRecordState(snapshot)
  const dependent = op('client-b', 1, { id: 'shape:b', typeName: 'shape', text: '后置' }, 'b-1')
  dependent.deps = { 'client-a': 1 }

  const deferred = applyOperation(state, dependent)
  assert.equal(deferred.accepted, false)
  assert.equal(deferred.retryable, true)
  assert.equal(deferred.reason, 'missing_dependencies')
  assert.deepEqual(deferred.missing_dependencies, [{ clientId: 'client-a', clock: 1, observed: 0 }])
  assert.equal(state.records.has('shape:b'), false)
  assert.deepEqual(metadataFromRecordState(state).state_vector, {})

  assert.equal(applyOperation(state, op('client-a', 1, { id: 'shape:a2', typeName: 'shape', text: '前置' }, 'a-1')).accepted, true)
  assert.equal(applyOperation(state, dependent).accepted, true)
  assert.deepEqual(metadataFromRecordState(state).state_vector, { 'client-a': 1, 'client-b': 1 })
})

test('causal dependencies are checked in batch order without becoming rejections', () => {
  const state = createRecordState(snapshot)
  const prerequisite = op('client-a', 2, { id: 'shape:a3', typeName: 'shape' }, 'a-2')
  const dependent = op('client-b', 1, { id: 'shape:b3', typeName: 'shape' }, 'b-1')
  dependent.deps = { 'client-a': 2 }

  const result = applyOperations(state, [prerequisite, dependent])
  assert.equal(result.accepted.length, 2)
  assert.equal(result.deferred.length, 0)
  assert.equal(result.rejected.length, 0)
  assert.deepEqual(metadataFromRecordState(state).state_vector, { 'client-a': 2, 'client-b': 1 })
})

test('causal metadata survives snapshot restoration and malformed vectors are rejected', () => {
  const state = createRecordState(snapshot)
  const operation = op('client-a', 4, { id: 'shape:causal', typeName: 'shape', text: '持久化' }, 'a-4')
  assert.equal(applyOperation(state, operation).accepted, true)
  const metadata = metadataFromRecordState(state)
  const restored = createRecordState(snapshotFromRecordState(snapshot, state), metadata)

  const dependent = op('client-b', 1, { id: 'shape:causal-b', typeName: 'shape' }, 'b-1')
  dependent.deps = { 'client-a': 4 }
  assert.equal(applyOperation(restored, dependent).accepted, true)
  assert.equal(applyOperation(restored, { ...op('client-c', 1, { id: 'shape:bad', typeName: 'shape' }, 'c-1'), deps: { 'client-a': -1 } }).reason, 'invalid_operation')
})

test('shape fields use explicit categories while unrelated geometry/content fields still merge independently', () => {
  const state = createRecordState(snapshot)
  const result = applyOperation(state, op('geometry', 2, {
    id: 'shape:a', typeName: 'shape', x: 12, rotation: 0.5, text: '新内容', color: 'blue',
  }))
  assert.equal(result.accepted, true)
  assert.equal(state.records.get('shape:a').x, 12)
  assert.equal(state.records.get('shape:a').text, '新内容')
  assert.equal(state.records.get('shape:a').color, 'blue')
  assert.equal(shapeFieldCategory('x'), 'geometry')
  assert.equal(shapeFieldCategory('text'), 'content')
  assert.equal(shapeFieldCategory('color'), 'style')
  assert.equal(shapeMergeStrategy('parentId'), 'hierarchy-group-causal-lww')
})

test('parentId and index are resolved as one hierarchy group and report readable conflict metadata', () => {
  const state = createRecordState(snapshot)
  assert.equal(applyOperation(state, op('winner', 5, {
    id: 'shape:a', typeName: 'shape', parentId: 'page:one', index: 'a1', text: '较新',
  })).accepted, true)
  const stale = applyOperation(state, op('older', 4, {
    id: 'shape:a', typeName: 'shape', parentId: 'page:two', index: 'b1', text: '较旧',
  }))
  assert.equal(stale.accepted, false)
  assert.deepEqual(stale.conflicted_fields, ['parentId', 'index', 'text'])
  assert.equal(state.records.get('shape:a').parentId, 'page:one')
  assert.equal(state.records.get('shape:a').index, 'a1')
  assert.equal(state.records.get('shape:a').text, '较新')
  assert.equal(stale.conflict.field_categories.parentId, 'hierarchy')
  assert.equal(stale.conflict.merge_strategies.parentId, 'hierarchy-group-causal-lww')
  assert.equal(stale.conflict.field_categories.text, 'content')
})

test('hierarchy group metadata survives persistence and concurrent hierarchy edits are deterministic', () => {
  const state = createRecordState(snapshot)
  const first = op('client-a', 3, { id: 'shape:a', typeName: 'shape', parentId: 'page:a' })
  const concurrent = op('client-b', 3, { id: 'shape:a', typeName: 'shape', index: 'b1' })
  assert.equal(applyOperation(state, first).accepted, true)
  const result = applyOperation(state, concurrent)
  assert.equal(result.accepted, true)
  assert.deepEqual(result.conflicted_fields, [])
  assert.equal(state.records.get('shape:a').parentId, 'page:a')
  assert.equal(state.records.get('shape:a').index, 'b1')

  const metadata = metadataFromRecordState(state)
  assert.equal(metadata.shape_group_versions['shape:a'].hierarchy.clientId, 'client-b')
  const restored = createRecordState(snapshotFromRecordState(snapshot, state), metadata)
  const older = applyOperation(restored, op('client-c', 2, { id: 'shape:a', typeName: 'shape', parentId: 'page:c' }))
  assert.equal(older.accepted, false)
  assert.equal(older.reason, 'stale_operation')
  assert.equal(older.conflict.merge_strategies.parentId, 'hierarchy-group-causal-lww')
})

test('nested field patches merge independent TLDraw props concurrently', () => {
  const left = createRecordState(snapshot)
  const right = createRecordState(snapshot)
  const color = {
    ...op('client-a', 2, { id: 'shape:a', typeName: 'shape' }, 'props-color'),
    patches: [{ path: ['props', 'color'], value: 'red' }],
  }
  const size = {
    ...op('client-b', 2, { id: 'shape:a', typeName: 'shape' }, 'props-size'),
    patches: [{ path: ['props', 'size'], value: 4 }],
  }
  assert.equal(applyOperation(left, color).accepted, true)
  assert.equal(applyOperation(left, size).accepted, true)
  assert.equal(applyOperation(right, size).accepted, true)
  assert.equal(applyOperation(right, color).accepted, true)
  assert.deepEqual(left.records.get('shape:a'), right.records.get('shape:a'))
  assert.deepEqual(left.records.get('shape:a').props, { color: 'red', size: 4 })
})

test('nested field patch deletion and metadata survive snapshot restoration', () => {
  const state = createRecordState(snapshot)
  const first = {
    ...op('client-a', 3, { id: 'shape:a', typeName: 'shape' }, 'props-first'),
    patches: [
      { path: ['props', 'palette', 'primary'], value: '#fff' },
      { path: ['props', 'palette', 'secondary'], value: '#000' },
    ],
  }
  assert.equal(applyOperation(state, first).accepted, true)
  const removal = {
    ...op('client-a', 4, { id: 'shape:a', typeName: 'shape' }, 'props-remove'),
    patches: [{ path: ['props', 'palette', 'secondary'], unset: true }],
  }
  assert.equal(applyOperation(state, removal).accepted, true)
  assert.deepEqual(state.records.get('shape:a').props, { palette: { primary: '#fff' } })
  const metadata = metadataFromRecordState(state)
  assert.equal(metadata.field_versions['shape:a']['/props/palette/primary'].clientId, 'client-a')
  const restored = createRecordState(snapshotFromRecordState(snapshot, state), metadata)
  assert.equal(applyOperation(restored, {
    ...op('client-b', 2, { id: 'shape:a', typeName: 'shape' }, 'props-restored'),
    patches: [{ path: ['props', 'palette', 'accent'], value: '#0ff' }],
  }).accepted, true)
  assert.deepEqual(restored.records.get('shape:a').props, { palette: { primary: '#fff', accent: '#0ff' } })
})

test('a whole props replacement conflicts with overlapping nested patches', () => {
  const state = createRecordState(snapshot)
  const nested = {
    ...op('client-a', 5, { id: 'shape:a', typeName: 'shape' }, 'nested-props'),
    patches: [{ path: ['props', 'theme', 'primary'], value: 'blue' }],
  }
  const whole = op('client-b', 4, { id: 'shape:a', typeName: 'shape', props: { theme: { primary: 'green' } } }, 'whole-props')
  assert.equal(applyOperation(state, nested).accepted, true)
  const result = applyOperation(state, whole)
  assert.equal(result.accepted, false)
  assert.deepEqual(result.conflicted_fields, ['props'])
  assert.equal(result.conflict.field_categories.props, 'content')
  assert.deepEqual(state.records.get('shape:a').props, { theme: { primary: 'blue' } })
})

test('nested patch validation rejects unsafe and overlapping paths', () => {
  const state = createRecordState(snapshot)
  const unsafe = {
    ...op('client-a', 1, { id: 'shape:a', typeName: 'shape' }, 'unsafe'),
    patches: [{ path: ['props', '__proto__', 'polluted'], value: true }],
  }
  assert.equal(applyOperation(state, unsafe).reason, 'invalid_operation')
  const overlapping = {
    ...op('client-a', 2, { id: 'shape:a', typeName: 'shape' }, 'overlap'),
    patches: [
      { path: ['props'], value: { theme: 'dark' } },
      { path: ['props', 'theme'], value: 'light' },
    ],
  }
  assert.equal(applyOperation(state, overlapping).reason, 'invalid_operation')
})
