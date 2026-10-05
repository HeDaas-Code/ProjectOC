import { describe, expect, it } from 'vitest'
import {
  buildRetryOperation,
  conflictCategoryLabel,
  conflictStrategyLabel,
  getConflictLocalValue,
  getConflictRemoteValue,
  normalizeSyncConflicts,
  type SyncConflict,
} from '../conflicts'
import type { PendingOperation } from '../offlineOps'

const pendingPut: PendingOperation = {
  kind: 'put',
  clientId: 'client-a',
  clock: 4,
  opId: 'op-a-4',
  record: { id: 'shape:1', typeName: 'shape', x: 10, text: '本地' },
  unset: ['color'],
}

function conflict(): SyncConflict {
  return normalizeSyncConflicts(
    [{
      operation: pendingPut,
      reason: 'stale_operation',
      conflict: {
        fields: ['text', 'color'],
        current: { id: 'shape:1', typeName: 'shape', x: 30, text: '远端', color: 'blue' },
        field_versions: { text: { clock: 9, clientId: 'client-b', opId: 'op-b-9' } },
      },
    }],
    [],
    [pendingPut],
  )[0]
}

describe('sync conflict normalization', () => {
  it('normalizes server fields and keeps local/remote values distinct', () => {
    const item = conflict()
    expect(item.recordId).toBe('shape:1')
    expect(item.fields).toEqual(['text', 'color'])
    expect(getConflictLocalValue(item, 'text')).toBe('本地')
    expect(getConflictLocalValue(item, 'color')).toBe('删除字段')
    expect(getConflictRemoteValue(item, 'text')).toBe('远端')
    expect(getConflictRemoteValue(item, 'color')).toBe('blue')
    expect(item.fieldCategories).toEqual({ text: 'content', color: 'style' })
    expect(item.mergeStrategies).toEqual({ text: 'field-causal-lww', color: 'field-causal-lww' })
  })

  it('falls back to a pending operation and snapshot when server details are partial', () => {
    const result = normalizeSyncConflicts(
      [],
      [{ operation: { ...pendingPut }, reason: 'stale_operation' }],
      [pendingPut],
      { document: { store: { 'shape:1': { id: 'shape:1', typeName: 'shape', text: 'snapshot' } } } },
    )
    expect(result[0].fields).toEqual(['x', 'text', 'color'])
    expect(result[0].remote?.text).toBe('snapshot')
  })


  it('keeps server-provided shape categories and explains merge strategies', () => {
    const item = normalizeSyncConflicts([{
      operation: pendingPut,
      reason: 'stale_operation',
      conflict: {
        fields: ['x', 'parentId', 'text', 'color', 'meta'],
        field_categories: {
          x: 'geometry', parentId: 'hierarchy', text: 'content', color: 'style', meta: 'metadata',
        },
        merge_strategies: {
          x: 'field-causal-lww', parentId: 'hierarchy-group-causal-lww',
          text: 'field-causal-lww', color: 'field-causal-lww', meta: 'field-causal-lww',
        },
      },
    }])[0]
    expect(item.fieldCategories).toEqual({
      x: 'geometry', parentId: 'hierarchy', text: 'content', color: 'style', meta: 'metadata',
    })
    expect(conflictCategoryLabel(item.fieldCategories.parentId)).toBe('层级')
    expect(conflictStrategyLabel(item.mergeStrategies.parentId)).toBe('层级组因果 LWW')
  })

  it('falls back safely for an unknown server category', () => {
    const item = normalizeSyncConflicts([{
      operation: pendingPut,
      conflict: { fields: ['rotation'], field_categories: { rotation: 'future-category' } },
    }])[0]
    expect(item.fieldCategories.rotation).toBe('geometry')
    expect(item.mergeStrategies.rotation).toBe('field-causal-lww')
  })

  it('creates a fresh retry operation with only conflicted fields', () => {
    const item = conflict()
    let clock = 20
    const retry = buildRetryOperation(item, 'client-a', () => ++clock, value => `retry:${value}`, { 'client-b': 9 })
    expect(retry).toEqual({
      kind: 'put',
      clientId: 'client-a',
      clock: 21,
      opId: 'retry:21',
      deps: { 'client-b': 9 },
      record: { id: 'shape:1', typeName: 'shape', text: '本地' },
      unset: ['color'],
    })
  })

  it('creates a fresh remove retry for record-level conflicts', () => {
    const operation: PendingOperation = {
      kind: 'remove', clientId: 'client-a', clock: 1, opId: 'remove-1', recordId: 'shape:1',
    }
    const item = normalizeSyncConflicts(
      [{ operation, reason: 'stale_operation', conflicted_fields: ['__record__'] }],
      [],
      [operation],
    )[0]
    const retry = buildRetryOperation(item, 'client-a', () => 8, value => `retry:${value}`)
    expect(retry).toEqual({
      kind: 'remove', clientId: 'client-a', clock: 8, opId: 'retry:8', recordId: 'shape:1',
    })
  })
})

it('reads nested patch conflict values and preserves them in a retry', () => {
  const operation: PendingOperation = {
    kind: 'put', clientId: 'client-a', clock: 4, opId: 'nested-op',
    record: { id: 'shape:1', typeName: 'shape' },
    patches: [{ path: ['props', 'theme', 'primary'], value: 'blue' }],
  }
  const item = normalizeSyncConflicts([{
    operation,
    reason: 'stale_operation',
    conflict: {
      fields: ['/props/theme/primary'],
      current: { id: 'shape:1', typeName: 'shape', props: { theme: { primary: 'green' } } },
      field_categories: { '/props/theme/primary': 'content' },
    },
  }])[0]
  expect(getConflictLocalValue(item, '/props/theme/primary')).toBe('blue')
  expect(getConflictRemoteValue(item, '/props/theme/primary')).toBe('green')
  expect(item.fieldCategories['/props/theme/primary']).toBe('content')
  expect(buildRetryOperation(item, 'client-a', () => 10, value => `retry:${value}`)?.patches).toEqual([
    { path: ['props', 'theme', 'primary'], value: 'blue' },
  ])
})
