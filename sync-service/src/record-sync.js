/**
 * Deterministic, record-level collaboration protocol for TLDraw document stores.
 *
 * This is intentionally small and dependency-free.  It is not a snapshot
 * broadcast: each message carries a put/remove operation for one TLDraw
 * record, and concurrent fields are resolved independently using a
 * deterministic Lamport/LWW order.  The full snapshot is still materialized
 * for PostgreSQL persistence and backwards-compatible recovery.
 */

const BASE_VERSION = Object.freeze({ clock: 0, clientId: '', opId: '' })
const MAX_CAUSAL_CLIENTS = 256
const MAX_FIELD_PATCHES = 256
const MAX_FIELD_PATH_SEGMENTS = 32
const MAX_FIELD_PATH_LENGTH = 512

const SHAPE_FIELD_CATEGORIES = Object.freeze({
  geometry: new Set(['x', 'y', 'w', 'h', 'rotation']),
  content: new Set(['text', 'richText', 'props']),
  style: new Set(['color', 'fill', 'size', 'font', 'align']),
  hierarchy: new Set(['parentId', 'index']),
})

/** Explain the merge semantics used for a TLDraw shape field. */
function decodeJsonPointerSegment(segment) {
  return String(segment).replace(/~1/g, '/').replace(/~0/g, '~')
}

function fieldRoot(field) {
  if (typeof field !== 'string' || !field.startsWith('/')) return field
  const first = field.slice(1).split('/')[0]
  return decodeJsonPointerSegment(first)
}

export function shapeFieldCategory(field) {
  const root = fieldRoot(field)
  for (const [category, fields] of Object.entries(SHAPE_FIELD_CATEGORIES)) {
    if (fields.has(root)) return category
  }
  return 'metadata'
}

export function shapeMergeStrategy(field) {
  const category = shapeFieldCategory(field)
  if (category === 'hierarchy') return 'hierarchy-group-causal-lww'
  if (category === 'metadata') return 'field-causal-lww'
  return 'field-causal-lww'
}

function isShape(record) { return record?.typeName === 'shape' }
function isHierarchyField(field) { return SHAPE_FIELD_CATEGORIES.hierarchy.has(field) }

function validCausalVector(value) {
  if (value === undefined) return {}
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const entries = Object.entries(value)
  if (entries.length > MAX_CAUSAL_CLIENTS) return null
  const vector = {}
  for (const [clientId, clock] of entries) {
    if (typeof clientId !== 'string' || clientId.length < 1 || clientId.length > 128
      || !Number.isSafeInteger(clock) || clock < 0) return null
    vector[clientId] = clock
  }
  return vector
}

export function compareVersion(a, b) {
  const left = a || BASE_VERSION
  const right = b || BASE_VERSION
  if (left.clock !== right.clock) return left.clock - right.clock
  const client = String(left.clientId || '').localeCompare(String(right.clientId || ''))
  if (client !== 0) return client
  return String(left.opId || '').localeCompare(String(right.opId || ''))
}

export function isValidOperation(operation) {
  if (!operation || typeof operation !== 'object' || Array.isArray(operation)) return false
  if (operation.kind !== 'put' && operation.kind !== 'remove') return false
  if (typeof operation.clientId !== 'string' || operation.clientId.length < 1 || operation.clientId.length > 128) return false
  if (typeof operation.opId !== 'string' || operation.opId.length < 1 || operation.opId.length > 256) return false
  if (!Number.isSafeInteger(operation.clock) || operation.clock < 1) return false
  if (operation.deps !== undefined && validCausalVector(operation.deps) === null) return false
  if (operation.kind === 'put') {
    if (operation.unset !== undefined && (!Array.isArray(operation.unset) || operation.unset.some(key => typeof key !== 'string' || key === 'id' || key === 'typeName'))) return false
    if (operation.patches !== undefined && (!Array.isArray(operation.patches) || operation.patches.length > MAX_FIELD_PATCHES || operation.patches.some(patch => !validFieldPatch(patch)))) return false
    const validRecord = Boolean(operation.record && typeof operation.record === 'object' && !Array.isArray(operation.record)
      && typeof operation.record.id === 'string' && typeof operation.record.typeName === 'string')
    if (!validRecord) return false
    const entries = operationFieldEntries(operation)
    const payloadEntries = entries.filter(entry => entry.key !== 'id' && entry.key !== 'typeName')
    for (let index = 0; index < payloadEntries.length; index += 1) {
      for (let next = index + 1; next < payloadEntries.length; next += 1) {
        if (pathsOverlap(payloadEntries[index].path, payloadEntries[next].path)) return false
      }
    }
    return true
  }
  return typeof operation.recordId === 'string' && operation.recordId.length > 0 && operation.recordId.length <= 256
}

function versionOf(operation) {
  return { clock: operation.clock, clientId: operation.clientId, opId: operation.opId }
}

function clone(value) {
  return value === undefined ? value : JSON.parse(JSON.stringify(value))
}

function safePathSegment(segment) {
  return typeof segment === 'string' && segment.length > 0 && segment.length <= 128
    && segment !== '__proto__' && segment !== 'prototype' && segment !== 'constructor'
}

function validFieldPath(value) {
  if (!Array.isArray(value) || value.length < 2 || value.length > MAX_FIELD_PATH_SEGMENTS) return null
  if (value.some(segment => !safePathSegment(segment))) return null
  const length = value.reduce((total, segment) => total + segment.length + 1, 0)
  return length <= MAX_FIELD_PATH_LENGTH ? [...value] : null
}

function encodeJsonPointerSegment(segment) {
  return segment.replace(/~/g, '~0').replace(/\//g, '~1')
}

function fieldPathKey(path) {
  return path.length === 1 ? path[0] : `/${path.map(encodeJsonPointerSegment).join('/')}`
}

function fieldPathFromKey(key) {
  if (typeof key !== 'string' || !key.startsWith('/')) return [key]
  return key.slice(1).split('/').map(decodeJsonPointerSegment)
}

function pathsOverlap(left, right) {
  const length = Math.min(left.length, right.length)
  for (let index = 0; index < length; index += 1) {
    if (left[index] !== right[index]) return false
  }
  return true
}

function safeJsonValue(value) {
  try {
    return JSON.stringify(value) !== undefined
  } catch {
    return false
  }
}

function validFieldPatch(patch) {
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return false
  const path = validFieldPath(patch.path)
  if (!path) return false
  const hasValue = Object.prototype.hasOwnProperty.call(patch, 'value')
  const isUnset = patch.unset === true
  if (hasValue === isUnset) return false
  return !hasValue || safeJsonValue(patch.value)
}

function operationFieldEntries(operation) {
  if (operation.kind !== 'put') return []
  const entries = []
  for (const [field, value] of Object.entries(operation.record || {})) {
    entries.push({path: [field], key: field, value, unset: false})
  }
  for (const field of operation.unset || []) {
    entries.push({path: [field], key: field, unset: true})
  }
  for (const patch of operation.patches || []) {
    const path = [...patch.path]
    entries.push({path, key: fieldPathKey(path), value: patch.value, unset: patch.unset === true})
  }
  return entries
}

function operationPayloadFields(operation) {
  return [...new Set(operationFieldEntries(operation)
    .map(entry => entry.key)
    .filter(field => field !== 'id' && field !== 'typeName'))]
}

function setPathValue(target, path, value) {
  if (path.length === 1) {
    target[path[0]] = clone(value)
    return
  }
  let cursor = target
  for (let index = 0; index < path.length - 1; index += 1) {
    const segment = path[index]
    const nextSegment = path[index + 1]
    const current = cursor[segment]
    if (!current || typeof current !== 'object' || Array.isArray(current) !== /^\d+$/.test(nextSegment)) {
      cursor[segment] = /^\d+$/.test(nextSegment) ? [] : {}
    }
    cursor = cursor[segment]
  }
  cursor[path.at(-1)] = clone(value)
}

function deletePathValue(target, path) {
  if (path.length === 1) {
    delete target[path[0]]
    return
  }
  let cursor = target
  for (let index = 0; index < path.length - 1; index += 1) {
    cursor = cursor?.[path[index]]
    if (!cursor || typeof cursor !== 'object') return
  }
  if (Array.isArray(cursor) && /^\d+$/.test(path.at(-1))) cursor.splice(Number(path.at(-1)), 1)
  else delete cursor[path.at(-1)]
}

function overlappingFieldKeys(fields, path) {
  return [...fields.keys()]
    .filter(key => key !== 'id' && key !== 'typeName')
    .filter(key => pathsOverlap(fieldPathFromKey(key), path))
}

function clearDominatedDescendants(fields, contexts, path, incomingKey) {
  for (const key of [...fields.keys()]) {
    if (key === incomingKey || key === 'id' || key === 'typeName') continue
    const candidate = fieldPathFromKey(key)
    if (candidate.length > path.length && pathsOverlap(candidate, path)) {
      fields.delete(key)
      contexts.delete(key)
    }
  }
}

function operationFieldsForConflict(operation) {
  return operation.kind === 'remove' ? ['__record__'] : operationPayloadFields(operation)
}

function safeStore(snapshot) {
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) return {}
  if (!snapshot.document || typeof snapshot.document !== 'object' || Array.isArray(snapshot.document)) return {}
  if (!snapshot.document.store || typeof snapshot.document.store !== 'object' || Array.isArray(snapshot.document.store)) return {}
  return snapshot.document.store
}

/** Build protocol state from a persisted TLDraw editor snapshot. */
function validVersion(value) {
  return value && Number.isSafeInteger(value.clock) && value.clock >= 0 && typeof value.clientId === 'string' && typeof value.opId === 'string'
    ? { clock: value.clock, clientId: value.clientId, opId: value.opId }
    : BASE_VERSION
}

function versionContext(version) {
  if (!version?.clientId || !version.clock) return {}
  return {[version.clientId]: version.clock}
}

function operationContext(operation) {
  const context = validCausalVector(operation.deps) || {}
  context[operation.clientId] = Math.max(context[operation.clientId] || 0, operation.clock)
  return context
}

function vectorDominates(left, right) {
  const keys = new Set([...Object.keys(left || {}), ...Object.keys(right || {})])
  let strict = false
  for (const key of keys) {
    const leftValue = left?.[key] || 0
    const rightValue = right?.[key] || 0
    if (leftValue < rightValue) return false
    if (leftValue > rightValue) strict = true
  }
  return strict
}

// Return whether the incoming operation should win a field. Causal order is
// authoritative; only concurrent operations use deterministic Lamport order.
function compareCausalField(incomingVersion, incomingContext, previousVersion, previousContext) {
  if (!previousVersion) return 1
  const incoming = incomingContext || versionContext(incomingVersion)
  const previous = previousContext || versionContext(previousVersion)
  if (vectorDominates(incoming, previous)) return 1
  if (vectorDominates(previous, incoming)) return -1
  return compareVersion(incomingVersion, previousVersion)
}

/** Build protocol state from a persisted TLDraw editor snapshot and metadata. */
export function createRecordState(snapshot, metadata = {}) {
  const records = new Map()
  const fieldVersions = new Map()
  const fieldContexts = new Map()
  const persistedFields = metadata && typeof metadata.field_versions === 'object' ? metadata.field_versions : {}
  const persistedContexts = metadata && typeof metadata.field_contexts === 'object' ? metadata.field_contexts : {}
  const persistedShapeGroups = metadata && typeof metadata.shape_group_versions === 'object' ? metadata.shape_group_versions : {}
  const persistedShapeGroupContexts = metadata && typeof metadata.shape_group_contexts === 'object' ? metadata.shape_group_contexts : {}
  const shapeGroupVersions = new Map()
  const shapeGroupContexts = new Map()
  for (const [id, record] of Object.entries(safeStore(snapshot))) {
    if (!record || typeof record !== 'object' || Array.isArray(record)) continue
    records.set(id, clone(record))
    const fields = new Map()
    const contexts = new Map()
    const persisted = persistedFields[id] && typeof persistedFields[id] === 'object' ? persistedFields[id] : {}
    const persistedRecordContexts = persistedContexts[id] && typeof persistedContexts[id] === 'object' ? persistedContexts[id] : {}
    for (const key of Object.keys(record)) {
      const version = validVersion(persisted[key])
      fields.set(key, version)
      contexts.set(key, validCausalVector(persistedRecordContexts[key]) || versionContext(version))
    }
    fieldVersions.set(id, fields)
    fieldContexts.set(id, contexts)
    if (isShape(record)) {
      const persistedGroup = persistedShapeGroups[id]?.hierarchy
      const persistedGroupContext = persistedShapeGroupContexts[id]?.hierarchy
      const candidates = ['parentId', 'index'].map(field => [field, fields.get(field)]).filter(([, version]) => Boolean(version))
      const derivedEntry = candidates.reduce((winner, candidate) => !winner || compareVersion(candidate[1], winner[1]) > 0 ? candidate : winner, null)
      const derived = derivedEntry?.[1]
      const groupVersion = persistedGroup ? validVersion(persistedGroup) : (derived || BASE_VERSION)
      const groupContext = validCausalVector(persistedGroupContext) || (derivedEntry ? contexts.get(derivedEntry[0]) : versionContext(groupVersion))
      shapeGroupVersions.set(id, new Map([['hierarchy', groupVersion]]))
      shapeGroupContexts.set(id, new Map([['hierarchy', groupContext || versionContext(groupVersion)]]))
    }
  }
  const tombstones = new Map()
  const tombstoneContexts = new Map()
  const persistedTombstones = metadata && typeof metadata.tombstones === 'object' ? metadata.tombstones : {}
  const persistedTombstoneContexts = metadata && typeof metadata.tombstone_contexts === 'object' ? metadata.tombstone_contexts : {}
  for (const [id, versionValue] of Object.entries(persistedTombstones)) {
    const version = validVersion(versionValue)
    tombstones.set(id, version)
    tombstoneContexts.set(id, validCausalVector(persistedTombstoneContexts[id]) || versionContext(version))
  }
  const stateVector = new Map()
  const persistedVector = validCausalVector(metadata?.state_vector) || {}
  for (const [clientId, clock] of Object.entries(persistedVector)) stateVector.set(clientId, clock)
  // Older snapshots did not persist a state vector. Derive a conservative one
  // from the field/tombstone versions so upgrading does not reject valid
  // operations that causally follow already-materialized records.
  for (const fields of fieldVersions.values()) {
    for (const version of fields.values()) {
      if (version.clientId) stateVector.set(version.clientId, Math.max(stateVector.get(version.clientId) || 0, version.clock))
    }
  }
  for (const version of tombstones.values()) {
    if (version.clientId) stateVector.set(version.clientId, Math.max(stateVector.get(version.clientId) || 0, version.clock))
  }
  return { records, fieldVersions, fieldContexts, shapeGroupVersions, shapeGroupContexts, tombstones, tombstoneContexts, stateVector, appliedOps: new Map() }
}


export function metadataFromRecordState(state) {
  const fieldVersions = {}
  const fieldContexts = {}
  for (const [id, fields] of state.fieldVersions.entries()) {
    const persisted = {}
    const persistedContexts = {}
    const contexts = state.fieldContexts.get(id) || new Map()
    for (const [field, version] of fields.entries()) {
      if (compareVersion(version, BASE_VERSION) !== 0) {
        persisted[field] = version
        persistedContexts[field] = contexts.get(field) || versionContext(version)
      }
    }
    if (Object.keys(persisted).length) {
      fieldVersions[id] = persisted
      fieldContexts[id] = persistedContexts
    }
  }
  const shapeGroupVersions = {}
  const shapeGroupContexts = {}
  for (const [id, groups] of state.shapeGroupVersions.entries()) {
    const versions = {}
    const contexts = {}
    const stateContexts = state.shapeGroupContexts.get(id) || new Map()
    for (const [group, version] of groups.entries()) {
      if (compareVersion(version, BASE_VERSION) !== 0) {
        versions[group] = version
        contexts[group] = stateContexts.get(group) || versionContext(version)
      }
    }
    if (Object.keys(versions).length) {
      shapeGroupVersions[id] = versions
      shapeGroupContexts[id] = contexts
    }
  }
  const tombstones = Object.fromEntries([...state.tombstones.entries()].map(([id, version]) => [id, version]))
  const tombstoneContexts = Object.fromEntries([...state.tombstones.entries()].map(([id, version]) => [id, state.tombstoneContexts.get(id) || versionContext(version)]))
  return {
    protocol: 'records-v1',
    field_versions: fieldVersions,
    field_contexts: fieldContexts,
    shape_group_versions: shapeGroupVersions,
    shape_group_contexts: shapeGroupContexts,
    tombstones,
    tombstone_contexts: tombstoneContexts,
    state_vector: Object.fromEntries([...state.stateVector.entries()].sort(([left], [right]) => left.localeCompare(right))),
  }
}

export function snapshotFromRecordState(baseSnapshot, state) {
  const next = clone(baseSnapshot) || {}
  next.document = { ...(next.document || {}) }
  next.document.store = Object.fromEntries([...state.records.entries()].map(([id, record]) => [id, clone(record)]))
  return next
}

function rememberOp(state, operation, accepted) {
  const key = `${operation.clientId}:${operation.opId}`
  state.appliedOps.set(key, accepted)
  // A bounded dedupe window prevents a malicious client from growing memory
  // forever while retaining enough history for retries and reconnects.
  while (state.appliedOps.size > 20_000) state.appliedOps.delete(state.appliedOps.keys().next().value)
}

function missingDependencies(state, operation) {
  const deps = validCausalVector(operation.deps)
  if (!deps) return []
  return Object.entries(deps)
    .filter(([clientId, clock]) => (state.stateVector.get(clientId) || 0) < clock)
    .map(([clientId, clock]) => ({clientId, clock, observed: state.stateVector.get(clientId) || 0}))
}

function observeAcceptedOperation(state, operation) {
  const current = state.stateVector.get(operation.clientId) || 0
  if (operation.clock > current) state.stateVector.set(operation.clientId, operation.clock)
}

function hasHigherFieldVersion(state, id, version, context) {
  const fields = state.fieldVersions.get(id)
  if (!fields) return false
  const contexts = state.fieldContexts.get(id) || new Map()
  for (const [field, fieldVersion] of fields.entries()) {
    if (field === 'id' || field === 'typeName') continue
    if (compareCausalField(fieldVersion, contexts.get(field), version, context) > 0) return true
  }
  return false
}

function conflictDetails(state, id, fields, incomingRecord) {
  const versions = state.fieldVersions.get(id)
  const record = incomingRecord || state.records.get(id)
  const details = {
    recordId: id,
    fields: [...new Set(fields)],
    current: clone(state.records.get(id)),
    field_versions: Object.fromEntries(
      [...new Set(fields)]
        .filter(field => versions?.has(field))
        .map(field => [field, versions.get(field)]),
    ),
    field_contexts: Object.fromEntries(
      [...new Set(fields)]
        .filter(field => state.fieldContexts.get(id)?.has(field))
        .map(field => [field, state.fieldContexts.get(id).get(field)]),
    ),
  }
  if (isShape(record)) {
    details.field_categories = Object.fromEntries(details.fields.map(field => [field, field === '__record__' ? 'record' : shapeFieldCategory(field)]))
    details.merge_strategies = Object.fromEntries(details.fields.map(field => [field, field === '__record__' ? 'record-tombstone-causal-lww' : shapeMergeStrategy(field)]))
  }
  return details
}

/** Apply one operation.  Returns a stable result and never mutates the op. */
export function applyOperation(state, operation) {
  if (!isValidOperation(operation)) return { accepted: false, reason: 'invalid_operation' }
  const key = `${operation.clientId}:${operation.opId}`
  const previous = state.appliedOps.get(key)
  if (previous) return { ...previous, accepted: false, reason: 'duplicate', duplicate: true }

  const missing = missingDependencies(state, operation)
  if (missing.length) {
    return {
      accepted: false,
      retryable: true,
      reason: 'missing_dependencies',
      missing_dependencies: missing,
      operation: clone(operation),
    }
  }

  const version = versionOf(operation)
  const context = operationContext(operation)
  if (operation.kind === 'remove') {
    const id = operation.recordId
    const tombstone = state.tombstones.get(id)
    const tombstoneContext = state.tombstoneContexts.get(id)
    if (tombstone && compareCausalField(version, context, tombstone, tombstoneContext) <= 0) {
      const result = {
        accepted: false,
        reason: 'stale_operation',
        recordId: id,
        conflicted_fields: ['__record__'],
        conflict: conflictDetails(state, id, ['__record__']),
      }
      rememberOp(state, operation, result)
      return result
    }
    if (hasHigherFieldVersion(state, id, version, context)) {
      const fields = [...(state.fieldVersions.get(id)?.keys() || [])].filter(field => field !== 'id' && field !== 'typeName')
      const result = {
        accepted: false,
        reason: 'stale_operation',
        recordId: id,
        conflicted_fields: fields.length ? fields : ['__record__'],
        conflict: conflictDetails(state, id, fields.length ? fields : ['__record__']),
      }
      rememberOp(state, operation, result)
      return result
    }
    state.records.delete(id)
    state.tombstones.set(id, version)
    state.tombstoneContexts.set(id, context)
    observeAcceptedOperation(state, operation)
    const result = { accepted: true, operation: clone(operation), recordId: id, version }
    rememberOp(state, operation, result)
    return result
  }

  const record = operation.record
  const id = record.id
  const tombstone = state.tombstones.get(id)
  const tombstoneContext = state.tombstoneContexts.get(id)
  if (tombstone && compareCausalField(version, context, tombstone, tombstoneContext) <= 0) {
    const conflictedFields = operationPayloadFields(operation)
    const result = {
      accepted: false,
      reason: 'stale_operation',
      recordId: id,
      conflicted_fields: conflictedFields,
      conflict: conflictDetails(state, id, conflictedFields),
    }
    rememberOp(state, operation, result)
    return result
  }

  const current = state.records.get(id) || {}
  const fields = state.fieldVersions.get(id) || new Map()
  const contexts = state.fieldContexts.get(id) || new Map()
  const next = { ...current }
  const conflictedFields = []
  let changed = false
  const isNewRecord = !state.records.has(id)
  const entries = operationFieldEntries(operation)
  const shapeHierarchyFields = isShape(record)
    ? [...new Set(entries.filter(entry => entry.path.length === 1 && isHierarchyField(entry.path[0])).map(entry => entry.path[0]))]
    : []
  const groupVersions = state.shapeGroupVersions.get(id) || new Map()
  const groupContexts = state.shapeGroupContexts.get(id) || new Map()
  const hierarchyVersion = groupVersions.get('hierarchy')
  const hierarchyContext = groupContexts.get('hierarchy')
  const hierarchyWins = !shapeHierarchyFields.length || !hierarchyVersion
    || compareCausalField(version, context, hierarchyVersion, hierarchyContext) > 0

  for (const entry of entries) {
    const {path, key, value, unset} = entry
    const field = path.length === 1 ? path[0] : key
    // id/typeName identify a TLDraw record. They are immutable identity
    // fields, not user-editable LWW fields once the record exists.
    if (!isNewRecord && (field === 'id' || field === 'typeName')) continue
    if (path.length === 1 && isHierarchyField(field)) {
      if (!hierarchyWins) {
        conflictedFields.push(field)
        continue
      }
      if (unset) delete next[field]
      else next[field] = clone(value)
      fields.set(field, version)
      contexts.set(field, context)
      changed = true
      continue
    }

    const overlapping = overlappingFieldKeys(fields, path)
    const wins = overlapping.every(existingKey => compareCausalField(
      version,
      context,
      fields.get(existingKey),
      contexts.get(existingKey),
    ) > 0)
    if (!wins) {
      conflictedFields.push(key)
      continue
    }
    if (unset) deletePathValue(next, path)
    else setPathValue(next, path, value)
    clearDominatedDescendants(fields, contexts, path, key)
    fields.set(key, version)
    contexts.set(key, context)
    changed = true
  }
  if (changed) {
    if (shapeHierarchyFields.length && hierarchyWins) {
      const nextGroups = state.shapeGroupVersions.get(id) || new Map()
      const nextContexts = state.shapeGroupContexts.get(id) || new Map()
      nextGroups.set('hierarchy', version)
      nextContexts.set('hierarchy', context)
      state.shapeGroupVersions.set(id, nextGroups)
      state.shapeGroupContexts.set(id, nextContexts)
    }

    state.records.set(id, next)
    state.fieldVersions.set(id, fields)
    state.fieldContexts.set(id, contexts)
    if (tombstone && compareCausalField(version, context, tombstone, tombstoneContext) > 0) {
      state.tombstones.delete(id)
      state.tombstoneContexts.delete(id)
    }
    observeAcceptedOperation(state, operation)
  }
  const result = {
    accepted: changed,
    operation: clone(operation),
    recordId: id,
    version,
    reason: changed ? undefined : 'stale_operation',
    conflicted_fields: conflictedFields,
    conflict: conflictedFields.length ? conflictDetails(state, id, conflictedFields) : undefined,
  }
  rememberOp(state, operation, result)
  return result
}

export function applyOperations(state, operations) {
  if (!Array.isArray(operations)) return { accepted: [], rejected: [{ reason: 'invalid_operations' }], deferred: [], conflicts: [] }
  const accepted = []
  const rejected = []
  const deferred = []
  const conflicts = []
  for (const operation of operations.slice(0, 100)) {
    const result = applyOperation(state, operation)
    if (result.accepted) accepted.push(result.operation)
    else if (result.retryable) deferred.push({
      operation: clone(operation),
      reason: result.reason || 'deferred',
      missing_dependencies: result.missing_dependencies || [],
    })
    else rejected.push({ operation: clone(operation), reason: result.reason || 'rejected', recordId: result.recordId, conflicted_fields: result.conflicted_fields || [] })
    if (result.conflict) conflicts.push({ operation: clone(operation), reason: result.reason || 'conflict', ...result.conflict })
  }
  return { accepted, rejected, deferred, conflicts }
}
