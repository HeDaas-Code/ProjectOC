import type { CausalVector, FieldPatch, PendingOperation } from './offlineOps'

export type ConflictRecord = Record<string, unknown>

export type ConflictFieldCategory =
  | 'geometry'
  | 'content'
  | 'style'
  | 'hierarchy'
  | 'metadata'
  | 'record'

export type SyncConflict = {
  key: string
  reason: string
  recordId: string
  fields: string[]
  operation: PendingOperation
  remote?: ConflictRecord
  fieldVersions: Record<string, { clock: number; clientId: string; opId: string }>
  fieldCategories: Record<string, ConflictFieldCategory>
  mergeStrategies: Record<string, string>
}

function snapshotRecord(snapshot: unknown, recordId: string): ConflictRecord | undefined {
  const store = (snapshot as { document?: { store?: Record<string, ConflictRecord> } } | undefined)?.document?.store
  const record = store?.[recordId]
  return record && typeof record === 'object' ? record : undefined
}

function operationKey(operation: PendingOperation): string {
  return `${operation.clientId}:${operation.opId}`
}

function patchKey(path: string[]): string {
  return `/${path.map(segment => segment.replace(/~/g, '~0').replace(/\//g, '~1')).join('/')}`
}

function decodePatchKey(field: string): string[] {
  if (!field.startsWith('/')) return [field]
  return field.slice(1).split('/').map(segment => segment.replace(/~1/g, '/').replace(/~0/g, '~'))
}

function readPath(value: unknown, path: string[]): unknown {
  let cursor = value as any
  for (const segment of path) {
    if (cursor === null || cursor === undefined) return undefined
    cursor = cursor[segment]
  }
  return cursor
}

const KNOWN_CATEGORIES = new Set<ConflictFieldCategory>([
  'geometry',
  'content',
  'style',
  'hierarchy',
  'metadata',
  'record',
])

const categoryFields: Record<Exclude<ConflictFieldCategory, 'record' | 'metadata'>, Set<string>> = {
  geometry: new Set(['x', 'y', 'w', 'h', 'rotation']),
  content: new Set(['text', 'richText', 'props']),
  style: new Set(['color', 'fill', 'size', 'font', 'align']),
  hierarchy: new Set(['parentId', 'index']),
}

export function classifyConflictField(field: string): ConflictFieldCategory {
  if (field === '__record__') return 'record'
  const root = field.startsWith('/') ? decodePatchKey(field)[0] : field
  for (const [category, fields] of Object.entries(categoryFields) as Array<[Exclude<ConflictFieldCategory, 'record' | 'metadata'>, Set<string>]>) {
    if (fields.has(root)) return category
  }
  return 'metadata'
}

export function conflictCategoryLabel(category: ConflictFieldCategory): string {
  return {
    geometry: '几何',
    content: '内容',
    style: '样式',
    hierarchy: '层级',
    metadata: '元数据',
    record: '记录',
  }[category]
}

export function conflictStrategyLabel(strategy: string): string {
  if (strategy === 'field-causal-lww') return '字段级因果 LWW'
  if (strategy === 'hierarchy-group-causal-lww') return '层级组因果 LWW'
  if (strategy === 'record-tombstone-causal-lww') return '记录删除标记因果 LWW'
  return strategy || '未说明的合并策略'
}

function normalizeCategories(raw: unknown, fields: string[]): Record<string, ConflictFieldCategory> {
  const source = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
  return Object.fromEntries(fields.map(field => {
    const value = source[field]
    const category = typeof value === 'string' && KNOWN_CATEGORIES.has(value as ConflictFieldCategory)
      ? value as ConflictFieldCategory
      : classifyConflictField(field)
    return [field, category]
  }))
}

function normalizeStrategies(raw: unknown, fields: string[], categories: Record<string, ConflictFieldCategory>): Record<string, string> {
  const source = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
  return Object.fromEntries(fields.map(field => {
    const explicit = source[field]
    if (typeof explicit === 'string' && explicit) return [field, explicit]
    const category = categories[field]
    return [field, category === 'record'
      ? 'record-tombstone-causal-lww'
      : category === 'hierarchy' ? 'hierarchy-group-causal-lww' : 'field-causal-lww']
  }))
}

function conflictFields(operation: PendingOperation, raw: unknown): string[] {
  const payload = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
  const fields = Array.isArray(payload.fields)
    ? payload.fields
    : Array.isArray(payload.conflicted_fields)
      ? payload.conflicted_fields
      : []
  const names = fields.filter((field): field is string => typeof field === 'string')
  if (names.length) return [...new Set(names)]
  if (operation.kind === 'remove') return ['__record__']
  return [
    ...new Set([
      ...Object.keys(operation.record || {}).filter(field => field !== 'id' && field !== 'typeName'),
      ...(operation.unset || []),
      ...(operation.patches || []).map(patch => patchKey(patch.path)),
    ]),
  ]
}

/** Normalize server conflict/rejection payloads into the UI's stable shape. */
export function normalizeSyncConflicts(
  rawConflicts: unknown[] = [],
  rejected: unknown[] = [],
  pendingBefore: PendingOperation[] = [],
  snapshot?: unknown,
): SyncConflict[] {
  const byOperation = new Map<string, Record<string, unknown>>()
  for (const item of [...rawConflicts, ...rejected]) {
    if (!item || typeof item !== 'object') continue
    const raw = item as Record<string, unknown>
    const operation = raw.operation
    if (!operation || typeof operation !== 'object') continue
    const candidate = operation as PendingOperation
    if (typeof candidate.clientId !== 'string' || typeof candidate.opId !== 'string') continue
    const key = operationKey(candidate)
    if (!byOperation.has(key)) byOperation.set(key, raw)
  }

  const normalized: SyncConflict[] = []
  for (const [key, raw] of byOperation) {
    const operation = (raw.operation || pendingBefore.find(item => operationKey(item) === key)) as PendingOperation | undefined
    if (!operation) continue
    const recordId = operation.kind === 'remove'
      ? operation.recordId
      : typeof operation.record?.id === 'string' ? operation.record.id : ''
    if (!recordId) continue

    const nested = raw.conflict && typeof raw.conflict === 'object'
      ? raw.conflict as Record<string, unknown>
      : raw
    const current = nested.current && typeof nested.current === 'object'
      ? nested.current as ConflictRecord
      : snapshotRecord(snapshot, recordId)
    const fields = conflictFields(operation, nested)
    const fieldVersions = nested.field_versions && typeof nested.field_versions === 'object'
      ? nested.field_versions as SyncConflict['fieldVersions']
      : nested.fieldVersions && typeof nested.fieldVersions === 'object'
        ? nested.fieldVersions as SyncConflict['fieldVersions']
        : {}
    const fieldCategories = normalizeCategories(
      nested.field_categories ?? nested.fieldCategories,
      fields,
    )
    const mergeStrategies = normalizeStrategies(
      nested.merge_strategies ?? nested.mergeStrategies,
      fields,
      fieldCategories,
    )
    normalized.push({
      key,
      reason: typeof raw.reason === 'string' ? raw.reason : 'stale_operation',
      recordId,
      fields,
      operation,
      remote: current,
      fieldVersions,
      fieldCategories,
      mergeStrategies,
    })
  }
  return normalized
}

export function getConflictLocalValue(conflict: SyncConflict, field: string): unknown {
  if (conflict.operation.kind === 'remove') return '删除整条记录'
  if (conflict.operation.unset?.includes(field)) return '删除字段'
  if (field.startsWith('/')) {
    const patch = conflict.operation.patches?.find(item => patchKey(item.path) === field)
    if (patch?.unset) return '删除字段'
    if (patch) return patch.value
    return readPath(conflict.operation.record, decodePatchKey(field))
  }
  return conflict.operation.record?.[field]
}

export function getConflictRemoteValue(conflict: SyncConflict, field: string): unknown {
  if (field === '__record__') return conflict.remote
  if (field.startsWith('/')) return readPath(conflict.remote, decodePatchKey(field))
  return conflict.remote?.[field]
}

/** Build a fresh operation so an explicit retry always wins over the old losing clock. */
export function buildRetryOperation(
  conflict: SyncConflict,
  clientId: string,
  nextClock: () => number,
  makeOperationId: (clock: number) => string,
  causalDeps?: CausalVector,
): PendingOperation | undefined {
  const clock = nextClock()
  const deps = causalDeps && Object.keys(causalDeps).length ? { deps: { ...causalDeps } } : {}
  if (conflict.operation.kind === 'remove') {
    return {
      kind: 'remove',
      clientId,
      clock,
      opId: makeOperationId(clock),
      ...deps,
      recordId: conflict.recordId,
    }
  }

  const sourceRecord = conflict.operation.record
  if (!sourceRecord) return undefined
  const fields = new Set(conflict.fields)
  const record: ConflictRecord = {
    id: sourceRecord.id,
    typeName: sourceRecord.typeName,
  }
  for (const field of Object.keys(sourceRecord)) {
    if (fields.has(field)) record[field] = sourceRecord[field]
  }
  const unset = (conflict.operation.unset || []).filter(field => fields.has(field))
  const patches = (conflict.operation.patches || []).filter(patch => fields.has(patchKey(patch.path)))
  return {
    kind: 'put',
    clientId,
    clock,
    opId: makeOperationId(clock),
    ...deps,
    record,
    ...(unset.length ? { unset } : {}),
    ...(patches.length ? { patches } : {}),
  }
}
