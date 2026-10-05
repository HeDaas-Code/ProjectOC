import { describe, expect, it } from 'vitest'
import { acknowledgePendingOperations, compareOperationCausality, enqueuePendingOperations, filterRemoteOperationByPending, isPendingOperation, loadPendingOperations, PendingOperationQueueError, savePendingOperations, type PendingOperation } from '../offlineOps'

const one: PendingOperation = { kind: 'put', clientId: 'a', clock: 1, opId: 'one', record: { id: 'shape:one' } }
const two: PendingOperation = { kind: 'remove', clientId: 'a', clock: 2, opId: 'two', recordId: 'shape:two' }

describe('offline operation queue', () => {
  it('deduplicates operations and removes only acknowledged entries', () => {
    const queued = enqueuePendingOperations([one], [one, two])
    expect(queued).toEqual([one, two])
    expect(acknowledgePendingOperations(queued, ['a:one'])).toEqual([two])
    expect(acknowledgePendingOperations(queued, [], ['a:two'])).toEqual([one])
  })

  it('persists and recovers a queue from local storage', () => {
    const data = new Map<string, string>()
    const storage = {
      getItem: (key: string) => data.get(key) ?? null,
      setItem: (key: string, value: string) => data.set(key, value),
      removeItem: (key: string) => data.delete(key),
    } as unknown as Storage
    savePendingOperations(storage, 'ops', [one, two])
    expect(loadPendingOperations(storage, 'ops')).toEqual([one, two])
    savePendingOperations(storage, 'ops', [])
    expect(loadPendingOperations(storage, 'ops')).toEqual([])
  })

  it('rejects malformed operations before they reach local storage', () => {
    expect(isPendingOperation(one)).toBe(true)
    expect(isPendingOperation({ ...one, clock: -1 })).toBe(false)
    expect(isPendingOperation({ kind: 'put', clientId: 'a', clock: 1, opId: 'bad', record: null })).toBe(false)
    expect(() => enqueuePendingOperations([], [{ ...one, record: null } as unknown as PendingOperation])).toThrow(PendingOperationQueueError)
  })

  it('does not restore malformed or duplicate persisted entries', () => {
    const data = new Map<string, string>([['ops', JSON.stringify([one, one, { kind: 'unknown' }, two])]])
    const storage = { getItem: (key: string) => data.get(key) ?? null, setItem: () => {}, removeItem: () => {} } as unknown as Storage
    expect(loadPendingOperations(storage, 'ops')).toEqual([one, two])
  })

  it('rebases only the non-conflicting fields of an older remote operation', () => {
    const local: PendingOperation = { kind: 'put', clientId: 'local', clock: 8, opId: 'local-8', record: { id: 'shape:one', typeName: 'shape', text: '本地文本' } }
    const remote: PendingOperation = { kind: 'put', clientId: 'remote', clock: 7, opId: 'remote-7', record: { id: 'shape:one', typeName: 'shape', text: '远端文本', x: 42 } }
    expect(filterRemoteOperationByPending(remote, [local])).toEqual({
      ...remote,
      record: { id: 'shape:one', typeName: 'shape', x: 42 },
    })
  })


  it('uses causal order before Lamport order when rebasing pending edits', () => {
    const local: PendingOperation = {
      kind: 'put', clientId: 'local', clock: 1, opId: 'local-follow-up',
      deps: { remote: 10 }, record: { id: 'shape:one', typeName: 'shape', text: '本地因果后续' },
    }
    const remote: PendingOperation = {
      kind: 'put', clientId: 'remote', clock: 10, opId: 'remote-base',
      record: { id: 'shape:one', typeName: 'shape', text: '远端基线', x: 42 },
    }
    expect(compareOperationCausality(local, remote)).toBeGreaterThan(0)
    expect(filterRemoteOperationByPending(remote, [local])).toEqual({
      ...remote,
      record: { id: 'shape:one', typeName: 'shape', x: 42 },
    })
  })

  it('keeps deterministic ordering for concurrent pending edits', () => {
    const local: PendingOperation = {
      kind: 'put', clientId: 'local', clock: 4, opId: 'local-concurrent',
      record: { id: 'shape:one', typeName: 'shape', text: '本地并发' },
    }
    const remote: PendingOperation = {
      kind: 'put', clientId: 'remote', clock: 5, opId: 'remote-concurrent',
      record: { id: 'shape:one', typeName: 'shape', text: '远端并发', x: 42 },
    }
    expect(compareOperationCausality(local, remote)).toBeLessThan(0)
    expect(filterRemoteOperationByPending(remote, [local])).toEqual(remote)
  })

  it('does not let a pending remove get resurrected by an older remote put', () => {
    const local: PendingOperation = { kind: 'remove', clientId: 'local', clock: 5, opId: 'local-remove', recordId: 'shape:one' }
    const remote: PendingOperation = { kind: 'put', clientId: 'remote', clock: 4, opId: 'remote-put', record: { id: 'shape:one', typeName: 'shape', text: '旧版本' } }
    expect(filterRemoteOperationByPending(remote, [local])).toBeUndefined()
  })
})

it('rebases nested props patches independently while preserving non-conflicting remote fields', () => {
  const local: PendingOperation = {
    kind: 'put', clientId: 'local', clock: 8, opId: 'local-props',
    record: { id: 'shape:one', typeName: 'shape' },
    patches: [{ path: ['props', 'theme', 'primary'], value: 'blue' }],
  }
  const remote: PendingOperation = {
    kind: 'put', clientId: 'remote', clock: 7, opId: 'remote-props',
    record: { id: 'shape:one', typeName: 'shape', x: 42 },
    patches: [
      { path: ['props', 'theme', 'primary'], value: 'green' },
      { path: ['props', 'theme', 'secondary'], value: 'gray' },
    ],
  }
  expect(filterRemoteOperationByPending(remote, [local])).toEqual({
    ...remote,
    patches: [{ path: ['props', 'theme', 'secondary'], value: 'gray' }],
  })
})

it('rejects unsafe or overlapping nested patches in the offline queue', () => {
  expect(isPendingOperation({
    kind: 'put', clientId: 'a', clock: 1, opId: 'unsafe', record: { id: 'shape:one' },
    patches: [{ path: ['props', '__proto__'], value: true }],
  })).toBe(false)
  expect(isPendingOperation({
    kind: 'put', clientId: 'a', clock: 1, opId: 'overlap', record: { id: 'shape:one' },
    patches: [
      { path: ['props', 'theme'], value: 'dark' },
      { path: ['props', 'theme', 'primary'], value: 'blue' },
    ],
  })).toBe(false)
})
