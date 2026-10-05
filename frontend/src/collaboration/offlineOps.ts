export type OperationVersion = { clock: number; clientId: string; opId: string }
export type CausalVector = Record<string, number>

export type FieldPatch = {
  path: string[]
  value?: unknown
  unset?: boolean
}

export type PendingOperation = {
  kind: 'put' | 'remove'
  clientId: string
  clock: number
  opId: string
  record?: Record<string, unknown>
  unset?: string[]
  patches?: FieldPatch[]
  recordId?: string
  deps?: CausalVector
}

/** Guardrails for the browser-side queue. A queue entry is temporary state, not a second database. */
export const MAX_PENDING_OPERATIONS = 2_000
export const MAX_PENDING_BYTES = 4 * 1024 * 1024
const MAX_FIELD_PATCHES = 256
const MAX_FIELD_PATH_SEGMENTS = 32
const FORBIDDEN_PATH_SEGMENTS = new Set(['__proto__', 'prototype', 'constructor'])

export class PendingOperationQueueError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'PendingOperationQueueError'
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function isCausalVector(value: unknown): value is CausalVector {
  if (!isRecord(value) || Object.keys(value).length > 256) return false
  return Object.entries(value).every(([clientId, clock]) =>
    clientId.length > 0 && clientId.length <= 128 && Number.isSafeInteger(clock) && Number(clock) >= 0,
  )
}

function validFieldPath(value: unknown): value is string[] {
  return Array.isArray(value) && value.length >= 2 && value.length <= MAX_FIELD_PATH_SEGMENTS
    && value.every(segment => typeof segment === 'string' && segment.length > 0 && segment.length <= 128 && !FORBIDDEN_PATH_SEGMENTS.has(segment))
}

function isJsonValue(value: unknown): boolean {
  try { return JSON.stringify(value) !== undefined } catch { return false }
}

function isFieldPatch(value: unknown): value is FieldPatch {
  if (!isRecord(value) || !validFieldPath(value.path)) return false
  const hasValue = Object.prototype.hasOwnProperty.call(value, 'value')
  const unset = value.unset === true
  return hasValue !== unset && (!hasValue || isJsonValue(value.value))
}

function patchKey(path: string[]): string {
  return `/${path.map(segment => segment.replace(/~/g, '~0').replace(/\//g, '~1')).join('/')}`
}

function decodePatchKey(field: string): string[] {
  if (!field.startsWith('/')) return [field]
  return field.slice(1).split('/').map(segment => segment.replace(/~1/g, '/').replace(/~0/g, '~'))
}

function pathsOverlap(left: string[], right: string[]): boolean {
  const length = Math.min(left.length, right.length)
  return left.slice(0, length).every((segment, index) => segment === right[index])
}

function operationFields(operation: PendingOperation): Array<{ key: string; path: string[] }> {
  if (operation.kind !== 'put') return []
  return [
    ...Object.keys(operation.record || {}).map(key => ({ key, path: [key] })),
    ...(operation.unset || []).map(key => ({ key, path: [key] })),
    ...(operation.patches || []).map(patch => ({ key: patchKey(patch.path), path: patch.path })),
  ]
}

/** Strictly validate the records-v1 subset persisted by the offline queue. */
export function isPendingOperation(value: unknown): value is PendingOperation {
  if (!isRecord(value)) return false
  if (value.kind !== 'put' && value.kind !== 'remove') return false
  if (typeof value.clientId !== 'string' || value.clientId.length < 1 || value.clientId.length > 200) return false
  if (typeof value.opId !== 'string' || value.opId.length < 1 || value.opId.length > 300) return false
  if (!Number.isSafeInteger(value.clock) || Number(value.clock) < 1) return false
  if (value.deps !== undefined && !isCausalVector(value.deps)) return false
  if (value.unset !== undefined && (!Array.isArray(value.unset) || value.unset.some(field => typeof field !== 'string' || !field))) return false
  if (value.patches !== undefined && (!Array.isArray(value.patches) || value.patches.length > MAX_FIELD_PATCHES || value.patches.some(patch => !isFieldPatch(patch)))) return false
  if (value.kind === 'put') {
    if (!isRecord(value.record) || typeof value.record.id !== 'string' || !value.record.id) return false
    if (value.record.typeName !== undefined && typeof value.record.typeName !== 'string') return false
    const fields = operationFields(value as unknown as PendingOperation)
      .filter(field => field.key !== 'id' && field.key !== 'typeName')
    for (let index = 0; index < fields.length; index += 1) {
      for (let next = index + 1; next < fields.length; next += 1) {
        if (pathsOverlap(fields[index].path, fields[next].path)) return false
      }
    }
    return true
  }
  return typeof value.recordId === 'string' && value.recordId.length > 0
}

export function operationKey(operation: Pick<PendingOperation, 'clientId' | 'opId'>): string {
  return `${operation.clientId}:${operation.opId}`
}

/**
 * The server orders fields by the same Lamport/LWW tuple. Keeping the
 * comparator in the browser lets us avoid painting an older remote field over
 * an optimistic offline edit that is still waiting for durable acknowledgement.
 */
export function compareOperationVersion(left: OperationVersion | undefined, right: OperationVersion | undefined): number {
  const a = left || { clock: 0, clientId: '', opId: '' }
  const b = right || { clock: 0, clientId: '', opId: '' }
  if (a.clock !== b.clock) return a.clock - b.clock
  const client = a.clientId.localeCompare(b.clientId)
  return client || a.opId.localeCompare(b.opId)
}

function operationContext(operation: PendingOperation): CausalVector {
  const context: CausalVector = { ...(operation.deps || {}) }
  context[operation.clientId] = Math.max(context[operation.clientId] || 0, operation.clock)
  return context
}

function vectorDominates(left: CausalVector, right: CausalVector): boolean {
  const actors = new Set([...Object.keys(left), ...Object.keys(right)])
  let strict = false
  for (const actor of actors) {
    const leftClock = left[actor] || 0
    const rightClock = right[actor] || 0
    if (leftClock < rightClock) return false
    if (leftClock > rightClock) strict = true
  }
  return strict
}

/**
 * Match the server's causal-first field ordering for optimistic rebasing.
 * Causally newer operations win even when their Lamport number is lower;
 * concurrent operations fall back to the deterministic version tuple.
 */
export function compareOperationCausality(left: PendingOperation, right: PendingOperation): number {
  const leftContext = operationContext(left)
  const rightContext = operationContext(right)
  if (vectorDominates(leftContext, rightContext)) return 1
  if (vectorDominates(rightContext, leftContext)) return -1
  return compareOperationVersion(
    { clock: left.clock, clientId: left.clientId, opId: left.opId },
    { clock: right.clock, clientId: right.clientId, opId: right.opId },
  )
}

function touchesField(operation: PendingOperation, field: string): boolean {
  if (operation.kind === 'remove') return true
  const path = decodePatchKey(field)
  return operationFields(operation).some(candidate => pathsOverlap(candidate.path, path))
}

/**
 * Remove fields from a remote operation that are dominated by a still-pending
 * local operation. This is an optimistic rebase guard, not a replacement for
 * server arbitration: the pending operation is still sent and the server's
 * durable result remains authoritative.
 */
export function filterRemoteOperationByPending(
  operation: PendingOperation,
  pending: PendingOperation[],
): PendingOperation | undefined {
  const sameRecord = pending.filter(candidate => {
    const candidateId = candidate.kind === 'remove' ? candidate.recordId : candidate.record?.id
    const operationId = operation.kind === 'remove' ? operation.recordId : operation.record?.id
    return candidateId && operationId && candidateId === operationId
  })
  if (!sameRecord.length) return operation
  if (operation.kind === 'remove') {
    if (sameRecord.some(candidate => compareOperationCausality(candidate, operation) >= 0)) return undefined
    return operation
  }
  const record = { ...(operation.record || {}) }
  const unset = [...(operation.unset || [])]
  const patches = [...(operation.patches || [])]
  for (const candidate of sameRecord) {
    if (compareOperationCausality(candidate, operation) < 0) continue
    if (candidate.kind === 'remove') return undefined
    for (const field of Object.keys(record)) {
      if (field !== 'id' && field !== 'typeName' && touchesField(candidate, field)) delete record[field]
    }
    for (let index = unset.length - 1; index >= 0; index -= 1) {
      if (touchesField(candidate, unset[index])) unset.splice(index, 1)
    }
    for (let index = patches.length - 1; index >= 0; index -= 1) {
      if (touchesField(candidate, patchKey(patches[index].path))) patches.splice(index, 1)
    }
  }
  const hasPayload = Object.keys(record).some(field => field !== 'id' && field !== 'typeName') || unset.length > 0 || patches.length > 0
  return hasPayload ? {
    ...operation,
    record: { id: operation.record?.id, typeName: operation.record?.typeName, ...record },
    ...(unset.length ? { unset } : {}),
    ...(patches.length ? { patches } : {}),
  } : undefined
}

function encodedBytes(value: string): number {
  return typeof TextEncoder === 'undefined' ? value.length : new TextEncoder().encode(value).byteLength
}

function assertQueueSize(operations: PendingOperation[]): void {
  if (operations.length > MAX_PENDING_OPERATIONS) {
    throw new PendingOperationQueueError(`离线操作队列已达到上限（${MAX_PENDING_OPERATIONS}），请恢复网络后重试`)
  }
  const encoded = JSON.stringify(operations)
  if (encodedBytes(encoded) > MAX_PENDING_BYTES) {
    throw new PendingOperationQueueError(`离线操作队列超过 ${Math.round(MAX_PENDING_BYTES / 1024 / 1024)} MB，请恢复网络后重试`)
  }
}

/** Add operations without duplicating an operation already persisted locally. */
export function enqueuePendingOperations(
  current: PendingOperation[],
  incoming: PendingOperation[],
): PendingOperation[] {
  const next = [...current]
  const known = new Set(current.map(operationKey))
  for (const operation of incoming) {
    if (!isPendingOperation(operation)) throw new PendingOperationQueueError('收到无法识别的离线操作，已拒绝缓存')
    const key = operationKey(operation)
    if (!known.has(key)) {
      next.push(operation)
      known.add(key)
    }
  }
  assertQueueSize(next)
  return next
}

/** Remove only operations durably acknowledged or explicitly rejected by the server. */
export function acknowledgePendingOperations(
  current: PendingOperation[],
  accepted: string[] = [],
  rejected: Array<{ clientId?: string; opId?: string } | string> = [],
): PendingOperation[] {
  const done = new Set(accepted)
  for (const item of rejected) {
    const key = typeof item === 'string' ? item : item.clientId && item.opId ? `${item.clientId}:${item.opId}` : ''
    if (key) done.add(key)
  }
  return current.filter(operation => !done.has(operationKey(operation)))
}

export function loadPendingOperations(storage: Storage, key: string): PendingOperation[] {
  try {
    const raw = storage.getItem(key)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    const known = new Set<string>()
    const valid = parsed.filter(isPendingOperation).filter(operation => {
      const id = operationKey(operation)
      if (known.has(id)) return false
      known.add(id)
      return true
    })
    // Keep the oldest entries so the queue remains deterministic; malformed or excess
    // local data must never prevent the canvas from opening.
    return valid.slice(0, MAX_PENDING_OPERATIONS)
  } catch {
    return []
  }
}

export function savePendingOperations(storage: Storage, key: string, operations: PendingOperation[]): void {
  if (!operations.length) {
    storage.removeItem(key)
    return
  }
  assertQueueSize(operations)
  try {
    storage.setItem(key, JSON.stringify(operations))
  } catch (error) {
    throw new PendingOperationQueueError('浏览器本地存储空间不足，离线操作仍保留在当前页面，请立即恢复网络')
  }
}
