<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from "vue";
import { createRoot, type Root } from "react-dom/client";
import React from "react";
import { Tldraw, type Editor } from "@tldraw/tldraw";
import { OfficialCollaborativeCanvas } from "../canvas/OfficialCollaborativeCanvas";
import "@tldraw/tldraw/tldraw.css";
import "katex/dist/katex.min.css";
import {
  CardUtil,
  TldrawAdapter,
  setProposals,
  type Card,
} from "../canvas/adapter";
import type { Canvas, Proposal } from "../types";
import { api, ApiError } from "../services/api";
import { SaveQueue } from "../services/autosave";
import {
  mergeCollaborator,
  presenceColor,
  presenceName,
  removeCollaborator,
  type Collaborator,
} from "../collaboration/presence";
import {
  acknowledgePendingOperations,
  enqueuePendingOperations,
  filterRemoteOperationByPending,
  loadPendingOperations,
  operationKey,
  PendingOperationQueueError,
  savePendingOperations,
  type CausalVector,
  type FieldPatch,
  type PendingOperation,
} from "../collaboration/offlineOps";
import {
  buildRetryOperation,
  conflictCategoryLabel,
  conflictStrategyLabel,
  getConflictLocalValue,
  getConflictRemoteValue,
  normalizeSyncConflicts,
  type SyncConflict,
} from "../collaboration/conflicts";
const props = defineProps<{ canvas: Canvas; proposals: Proposal[] }>();
const emit = defineEmits<{
  saved: [canvas: Canvas];
  error: [message: string];
}>();
const host = ref<HTMLElement>(),
  status = ref("已保存"),
  editing = ref(false),
  text = ref(""),
  kind = ref("markdown"),
  selected = ref("");
const syncStatus = ref("连接中…"),
  officialSyncActive = ref(false),
  syncRole = ref<"owner" | "editor" | "reader">(),
  collaborators = ref<Collaborator[]>([]),
  pendingCount = ref(0),
  pendingNotice = ref("");
const syncConflicts = ref<SyncConflict[]>([]);
const canEdit = computed(
  () => syncRole.value === "owner" || syncRole.value === "editor",
);
const isReadOnly = computed(() => !canEdit.value);
// The old records-v1 queue used `status` for REST save progress. Official
// TLDraw sync-v2 has its own durable room state in `syncStatus`; keep the
// legacy ref for the fallback path, but never render its misleading
// "待同步" label while an official room is active.
const displayedCanvasStatus = computed(() => {
  // `officialSync` is deliberately a transport guard (plain state) and is
  // not itself a Vue dependency. Use the reactive sync label as the signal
  // here so the footer also updates after the async ticket handshake.
  if (
    officialSyncActive.value ||
    syncStatus.value.includes("CRDT") ||
    syncStatus.value === "实时协作已连接"
  ) {
    if (syncStatus.value.includes("失败") || syncStatus.value.includes("断开"))
      return "实时协作待重连";
    if (syncStatus.value.includes("同步中") || syncStatus.value.includes("持久化"))
      return syncStatus.value;
    return "实时协作已连接";
  }
  return status.value;
});
function setEditorReadOnly(readonly: boolean) {
  if (adapter) adapter.editor.updateInstanceState({ isReadonly: readonly });
}
watch(syncRole, (role) => setEditorReadOnly(!role || role === "reader"));
type RecordValue = Record<string, any>;
type RecordsDiff = {
  added: Record<string, RecordValue>;
  updated: Record<string, [RecordValue, RecordValue]>;
  removed: Record<string, RecordValue>;
};
type WireOperation =
  | {
      kind: "put";
      clientId: string;
      clock: number;
      opId: string;
      deps?: CausalVector;
      record: RecordValue;
      unset?: string[];
      patches?: FieldPatch[];
    }
  | {
      kind: "remove";
      clientId: string;
      clock: number;
      opId: string;
      deps?: CausalVector;
      recordId: string;
    };
let root: Root,
  adapter: TldrawAdapter,
  dispose: (() => void) | undefined,
  socket: WebSocket | undefined,
  reconnectTimer: ReturnType<typeof setTimeout> | undefined,
  ticketRefreshTimer: ReturnType<typeof setTimeout> | undefined,
  presenceTimer: ReturnType<typeof setTimeout> | undefined,
  syncSuppress = false,
  disposed = false,
  officialSync = false,
  latestLocalSnapshot: unknown,
  version = props.canvas.snapshot_version;
const key = `oc:draft:${props.canvas.id}`;
const clientKey = `oc:sync-client:${props.canvas.id}`;
const clockKey = `oc:sync-clock:${props.canvas.id}`;
const operationCursorKey = `oc:sync-operation-cursor:${props.canvas.id}`;
const causalVectorKey = `oc:sync-causal-vector:${props.canvas.id}`;
const clientId =
  sessionStorage.getItem(clientKey) ||
  (() => {
    const id = crypto.randomUUID();
    sessionStorage.setItem(clientKey, id);
    return id;
  })();
const pendingOperationsKey = `oc:sync-pending-ops:${props.canvas.id}`;
let pendingOperations: PendingOperation[] = loadPendingOperations(
  localStorage,
  pendingOperationsKey,
);
pendingCount.value = pendingOperations.length;
let operationsInFlight = false;
let logicalClock = Number(sessionStorage.getItem(clockKey) || 0);
let operationCursor = Number(sessionStorage.getItem(operationCursorKey) ?? -1);
let causalVector: CausalVector = (() => {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(causalVectorKey) || "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
})();
function persistCausalVector() {
  sessionStorage.setItem(causalVectorKey, JSON.stringify(causalVector));
}
function updateCausalVector(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  for (const [actor, clock] of Object.entries(value as Record<string, unknown>)) {
    if (typeof actor !== "string" || !Number.isSafeInteger(clock) || Number(clock) < 0) continue;
    causalVector[actor] = Math.max(causalVector[actor] || 0, Number(clock));
  }
  persistCausalVector();
}
function observeLocalOperation(operation: WireOperation) {
  causalVector[operation.clientId] = Math.max(causalVector[operation.clientId] || 0, operation.clock);
  persistCausalVector();
}
function currentCausalDependencies(): CausalVector {
  return { ...causalVector };
}
function nextClock() {
  logicalClock += 1;
  sessionStorage.setItem(clockKey, String(logicalClock));
  return logicalClock;
}
function updateOperationCursor(value: unknown) {
  const next = Number(value);
  if (!Number.isSafeInteger(next) || next < 0) return;
  operationCursor = Math.max(operationCursor, next);
  sessionStorage.setItem(operationCursorKey, String(operationCursor));
}
function operationId(clock: number) {
  return `${clientId}:${clock}:${crypto.randomUUID()}`;
}
function persistPendingOperations() {
  savePendingOperations(localStorage, pendingOperationsKey, pendingOperations);
  pendingCount.value = pendingOperations.length;
}
function clearLegacyQueueAfterOfficialHandshake() {
  if (!pendingOperations.length) return;
  // These operations were produced by records-v1. They cannot be replayed into
  // a TLDraw sync-v2 room because the two protocols have different clocks and
  // conflict semantics. Keep a recoverable local copy for debugging, then
  // remove them from the active queue so the UI cannot report phantom pending
  // work or accidentally resend them after a reconnect.
  try {
    localStorage.setItem(
      `oc:legacy-pending-ops-backup:${props.canvas.id}`,
      JSON.stringify({
        protocol: "records-v1",
        canvasId: props.canvas.id,
        savedAt: new Date().toISOString(),
        operations: pendingOperations,
      }),
    );
  } catch {
    // A full localStorage must not prevent the official room from opening.
  }
  pendingOperations = [];
  persistPendingOperations();
}
function queuePendingOperations(operations: WireOperation[]) {
  pendingOperations = enqueuePendingOperations(
    pendingOperations,
    operations as PendingOperation[],
  );
  persistPendingOperations();
}
function acknowledgeOperations(
  accepted: string[] = [],
  rejected: unknown[] = [],
) {
  pendingOperations = acknowledgePendingOperations(
    pendingOperations,
    accepted,
    rejected as Array<string | { clientId?: string; opId?: string }>,
  );
  persistPendingOperations();
}
function rememberConflicts(
  rawConflicts: any[] = [],
  rejected: any[] = [],
  pendingBefore: PendingOperation[] = [],
  snapshot?: unknown,
) {
  const next = normalizeSyncConflicts(
    rawConflicts,
    rejected,
    pendingBefore,
    snapshot,
  );
  // records-v1 uses causal LWW to automatically merge high-frequency canvas
  // geometry updates. A stale move/resize/rotation is expected during normal
  // drawing and is not an actionable conflict for the user. Keep content,
  // hierarchy and record tombstone conflicts visible, while allowing the
  // official TLDraw CRDT to handle the common case without noisy warnings.
  const actionable = next.filter((item) => {
    if (item.reason !== "stale_operation") return true;
    return item.fields.some((field) => {
      const category = item.fieldCategories[field];
      return category === "content" || category === "hierarchy" || category === "record";
    });
  });
  if (!actionable.length) return;
  const keys = new Set(actionable.map((item) => item.key));
  syncConflicts.value = [
    ...syncConflicts.value.filter((item) => !keys.has(item.key)),
    ...actionable,
  ].slice(-20);
  pendingNotice.value = `检测到 ${actionable.length} 个需要处理的协作冲突，请审核本地值与远端值`;
}
function conflictLocalValue(conflict: SyncConflict, field: string): unknown {
  return getConflictLocalValue(conflict, field);
}
function conflictRemoteValue(conflict: SyncConflict, field: string): unknown {
  return getConflictRemoteValue(conflict, field);
}
function formatConflictValue(value: unknown): string {
  if (value === undefined) return "（不存在）";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
function dismissConflict(key: string) {
  syncConflicts.value = syncConflicts.value.filter((item) => item.key !== key);
  if (!syncConflicts.value.length) pendingNotice.value = "";
}
function retryConflict(conflict: SyncConflict) {
  if (!canEdit.value) return;
  const operation = buildRetryOperation(
    conflict,
    clientId,
    nextClock,
    operationId,
    currentCausalDependencies(),
  );
  if (!operation) return;
  observeLocalOperation(operation as WireOperation);
  sendRealtime([operation as WireOperation]);
  dismissConflict(conflict.key);
}
function jsonEqual(a: unknown, b: unknown) {
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}
function isPlainRecord(value: unknown): value is RecordValue {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
function collectNestedPatches(
  from: RecordValue | undefined,
  to: RecordValue | undefined,
  path: string[],
  patches: FieldPatch[],
) {
  const before = isPlainRecord(from) ? from : {};
  const after = isPlainRecord(to) ? to : {};
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  for (const key of keys) {
    const nextPath = [...path, key];
    const hasBefore = Object.prototype.hasOwnProperty.call(before, key);
    const hasAfter = Object.prototype.hasOwnProperty.call(after, key);
    if (!hasAfter) {
      patches.push({ path: nextPath, unset: true });
    } else if (!hasBefore) {
      patches.push({ path: nextPath, value: after[key] });
    } else if (isPlainRecord(before[key]) && isPlainRecord(after[key])) {
      collectNestedPatches(before[key], after[key], nextPath, patches);
    } else if (!jsonEqual(before[key], after[key])) {
      patches.push({ path: nextPath, value: after[key] });
    }
  }
}
function makePutOperation(
  record: RecordValue,
  unset: string[] = [],
  patches: FieldPatch[] = [],
): WireOperation {
  const clock = nextClock();
  const operation: WireOperation = {
    kind: "put",
    clientId,
    clock,
    opId: operationId(clock),
    deps: currentCausalDependencies(),
    record,
    ...(unset.length ? { unset } : {}),
    ...(patches.length ? { patches } : {}),
  };
  observeLocalOperation(operation);
  return operation;
}
function makeOperations(changes: RecordsDiff): WireOperation[] {
  const operations: WireOperation[] = [];
  for (const record of Object.values(changes.added || {}))
    operations.push(makePutOperation(record));
  for (const [from, to] of Object.values(changes.updated || {})) {
    const patch: RecordValue = { id: to.id, typeName: to.typeName };
    const patches: FieldPatch[] = [];
    for (const [field, value] of Object.entries(to)) {
      if (field === "id" || field === "typeName") continue;
      if (field === "props" && isPlainRecord(from[field]) && isPlainRecord(value)) {
        collectNestedPatches(from[field], value, [field], patches);
      } else if (!jsonEqual(from[field], value)) {
        patch[field] = value;
      }
    }
    const unset = Object.keys(from).filter(
      (field) => field !== "id" && field !== "typeName" && !(field in to),
    );
    if (Object.keys(patch).length > 2 || unset.length || patches.length)
      operations.push(makePutOperation(patch, unset, patches));
  }
  for (const record of Object.values(changes.removed || {})) {
    const clock = nextClock();
    const operation: WireOperation = {
      kind: "remove",
      clientId,
      clock,
      opId: operationId(clock),
      deps: currentCausalDependencies(),
      recordId: record.id,
    };
    operations.push(operation);
    observeLocalOperation(operation);
  }
  return operations;
}
const queue = new SaveQueue(
  async (snapshot) => {
    status.value = "保存中…";
    const saved = await api<Canvas>(`canvases/${props.canvas.id}/`, "PATCH", {
      snapshot,
      expected_version: version,
    });
    version = saved.snapshot_version;
    emit("saved", saved);
    status.value = "已保存";
    const local = localStorage.getItem(key);
    if (local && JSON.parse(local).snapshot === JSON.stringify(snapshot))
      localStorage.removeItem(key);
  },
  (e) => {
    status.value =
      e instanceof ApiError && e.status === 409
        ? "版本冲突 · 本地副本已保留"
        : "离线 / 待同步";
    emit("error", String(e));
  },
);
function flushRealtimeOperations() {
  if (
    !canEdit.value ||
    socket?.readyState !== WebSocket.OPEN ||
    !pendingOperations.length ||
    operationsInFlight
  )
    return false;
  try {
    socket.send(
      JSON.stringify({ type: "ops", ops: pendingOperations.slice(0, 100) }),
    );
    operationsInFlight = true;
    syncStatus.value = `实时同步中 · 离线队列 ${pendingOperations.length}`;
    return true;
  } catch {
    operationsInFlight = false;
    return false;
  }
}
function sendRealtime(operations: WireOperation[]) {
  if (!operations.length) return false;
  try {
    queuePendingOperations(operations);
    pendingNotice.value = "";
    return flushRealtimeOperations();
  } catch (error) {
    pendingNotice.value =
      error instanceof PendingOperationQueueError
        ? error.message
        : "离线操作无法缓存，请立即恢复网络并导出画布";
    syncStatus.value = "离线队列已暂停";
    emit("error", pendingNotice.value);
    return false;
  }
}
function sendPresence(
  state: {
    cursor?: { x: number; y: number } | null;
    selection?: string[];
  } = {},
) {
  if (!canEdit.value && syncRole.value !== "reader") return;
  if (socket?.readyState !== WebSocket.OPEN) return;
  clearTimeout(presenceTimer);
  presenceTimer = setTimeout(() => {
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify({ type: "presence", state }));
  }, 80);
}
function changed(changes?: RecordsDiff) {
  if (syncSuppress) return;
  const snapshot = adapter.getSnapshot();
  latestLocalSnapshot = snapshot;
  sendPresence({ selection: adapter.editor.getSelectedShapeIds() });
  if (!canEdit.value) {
    status.value = "只读";
    return;
  }
  try {
    localStorage.setItem(
      key,
      JSON.stringify({ version, snapshot: JSON.stringify(snapshot) }),
    );
  } catch {
    emit("error", "本地存储空间不足，请立即导出画布");
  }
  // The official TLDraw client owns CRDT transport and durable persistence.
  // Never create records-v1 operations for the same editor instance. In
  // particular, do not expose the legacy "待同步" state here: it makes a
  // healthy CRDT edit look like a pending/conflicting records-v1 operation.
  if (officialSync) {
    queue.clearPending();
    if (syncStatus.value === "实时协作已连接" || syncStatus.value === "官方 CRDT 已连接") {
      status.value = "实时协作已连接";
    } else if (syncStatus.value.includes("失败") || syncStatus.value.includes("断开")) {
      status.value = "实时协作待重连";
    } else {
      status.value = "官方 CRDT 同步中…";
    }
    return;
  }
  status.value = "待同步";
  const operations = changes ? makeOperations(changes) : [];
  // A record change has a durable, replayable representation. Do not also
  // enqueue its full snapshot: a delayed REST write can race the websocket
  // operation and restore an older document after collaborators have advanced.
  // The operation queue is persisted locally while the sync service is down.
  if (operations.length) {
    sendRealtime(operations);
    queue.clearPending();
  } else {
    // Snapshot-only recovery/export paths still use the versioned REST fallback.
    queue.enqueue(snapshot);
  }
}
function cloneRecord(value: RecordValue): RecordValue {
  try { return JSON.parse(JSON.stringify(value)); } catch { return { ...value }; }
}
function applyFieldPatch(record: RecordValue, patch: FieldPatch) {
  if (!Array.isArray(patch.path) || patch.path.length < 2) return;
  let cursor: any = record;
  for (let index = 0; index < patch.path.length - 1; index += 1) {
    const segment = patch.path[index];
    const nextSegment = patch.path[index + 1];
    if (!cursor[segment] || typeof cursor[segment] !== "object")
      cursor[segment] = /^\d+$/.test(nextSegment) ? [] : {};
    cursor = cursor[segment];
  }
  const leaf = patch.path[patch.path.length - 1];
  if (patch.unset) {
    if (Array.isArray(cursor) && /^\d+$/.test(leaf)) cursor.splice(Number(leaf), 1);
    else delete cursor[leaf];
  } else cursor[leaf] = patch.value;
}
function applyOperationToRecord(
  current: RecordValue | undefined,
  operation: Extract<WireOperation, { kind: "put" }>,
): RecordValue {
  const merged = cloneRecord({ ...(current || {}), ...(operation.record || {}) });
  for (const field of operation.unset || []) delete merged[field];
  for (const patch of operation.patches || []) applyFieldPatch(merged, patch);
  return merged;
}
function applyPendingOperationsToEditor() {
  if (!adapter || !pendingOperations.length) return;
  for (const operation of pendingOperations) {
    if (operation.kind === "remove") {
      if (adapter.editor.store.get(operation.recordId as any))
        adapter.editor.store.remove([operation.recordId as any]);
      continue;
    }
    const current = adapter.editor.store.get(operation.record?.id as any) as
      RecordValue | undefined;
    adapter.editor.store.put([applyOperationToRecord(current, operation as Extract<WireOperation, { kind: "put" }>) as any]);
  }
}
function applyRemoteOperations(
  operations: WireOperation[],
  nextVersion?: number,
) {
  if (!adapter || !Array.isArray(operations)) return;
  syncSuppress = true;
  try {
    adapter.editor.store.mergeRemoteChanges(() => {
      for (const operation of operations) {
        const rebased = filterRemoteOperationByPending(
          operation as PendingOperation,
          pendingOperations,
        ) as WireOperation | undefined;
        if (!rebased) continue;
        if (rebased.kind === "remove") {
          if (adapter.editor.store.get(rebased.recordId as any))
            adapter.editor.store.remove([rebased.recordId as any]);
          continue;
        }
        const current = adapter.editor.store.get(rebased.record.id as any) as
          RecordValue | undefined;
        adapter.editor.store.put([applyOperationToRecord(current, rebased as Extract<WireOperation, { kind: "put" }>) as any]);
      }
    });
    if (typeof nextVersion === "number")
      version = Math.max(version, nextVersion);
    status.value = "已同步";
    if (!operationsInFlight && pendingOperations.length) flushRealtimeOperations();
  } catch {
    emit("error", "协作操作无法应用，已保留当前本地画布");
  } finally {
    setTimeout(() => {
      syncSuppress = false;
    }, 0);
  }
}
function applyRemoteSnapshot(
  snapshot: unknown,
  nextVersion?: number,
  nextCursor?: number,
) {
  if (!adapter) return;
  const incomingVersion =
    typeof nextVersion === "number" ? nextVersion : version;
  const incomingCursor =
    typeof nextCursor === "number" ? nextCursor : operationCursor;
  // Snapshots are a fallback/base materialization. Never let an older
  // snapshot erase operations that this client has already observed.
  if (
    incomingVersion < version ||
    (incomingVersion === version && incomingCursor < operationCursor)
  )
    return;
  syncSuppress = true;
  try {
    adapter.loadSnapshot(snapshot);
    // A rejected operation causes the server to return its authoritative
    // materialization. Rebase the still-pending local operations on top of it
    // so unrelated offline edits are not silently erased by that recovery.
    adapter.editor.store.mergeRemoteChanges(() =>
      applyPendingOperationsToEditor(),
    );
    version = Math.max(version, incomingVersion);
    updateOperationCursor(incomingCursor);
    status.value = "已同步";
  } catch {
    emit("error", "协作快照无法解析，已保留当前本地画布");
  } finally {
    setTimeout(() => {
      syncSuppress = false;
    }, 0);
  }
}
async function connectSync() {
  queue.pause();
  try {
    const ticket = await api<{
      ticket: string;
      url: string;
      role: "owner" | "editor" | "reader";
      expires_at: number;
    }>(`canvases/${props.canvas.id}/sync-ticket/`, "POST", {
      client_id: clientId,
    });
    syncRole.value = ticket.role;
    setEditorReadOnly(ticket.role === "reader");
    syncStatus.value = "连接中…";
    if (ticketRefreshTimer) clearTimeout(ticketRefreshTimer);
    const refreshIn = Math.max(
      10_000,
      ticket.expires_at * 1000 - Date.now() - 15_000,
    );
    ticketRefreshTimer = setTimeout(() => {
      if (!disposed) socket?.close();
    }, refreshIn);
    const separator = ticket.url.includes("?") ? "&" : "?";
    const replay =
      operationCursor >= 0
        ? `&after=${encodeURIComponent(operationCursor)}`
        : "";
    socket = new WebSocket(
      `${ticket.url}${separator}ticket=${encodeURIComponent(ticket.ticket)}${replay}`,
    );
    socket.onopen = () => {
      queue.pause();
      syncStatus.value = "实时协作已连接";
      sendPresence({
        selection: adapter?.editor.getSelectedShapeIds?.() || [],
      });
      flushRealtimeOperations();
    };
    socket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === "hello") {
          collaborators.value = message.collaborators || [];
          updateOperationCursor(message.operation_cursor);
          updateCausalVector(message.state_vector);
          // A fresh canvas can have no TLDraw records in PostgreSQL because
          // the editor creates its root/page records locally. Establish one
          // full baseline before sending record-level operations; otherwise a
          // later reload would only contain the changed shape and TLDraw could
          // not restore the document.
          if (message.snapshot && !message.needs_baseline)
            applyRemoteSnapshot(
              message.snapshot,
              message.version,
              message.operation_cursor,
            );
          if (
            message.needs_baseline &&
            canEdit.value &&
            adapter &&
            socket?.readyState === WebSocket.OPEN
          ) {
            socket.send(
              JSON.stringify({
                type: "baseline",
                snapshot: adapter.getSnapshot(),
                sync_metadata: { protocol: "records-v1", baseline_ready: true },
              }),
            );
          }
        } else if (message.type === "baseline_ack") {
          if (typeof message.version === "number") version = message.version;
          updateOperationCursor(message.operation_cursor);
          updateCausalVector(message.state_vector);
          // The accepted baseline contains every local record that existed
          // before the websocket became ready, so the REST fallback is no
          // longer needed for that same edit. If another client won the race,
          // keep pending record operations and replay them instead.
          if (message.accepted === true) queue.clearPending();
          if (message.accepted === false && message.snapshot) {
            queue.clearPending();
            applyRemoteSnapshot(
              message.snapshot,
              message.version,
              message.operation_cursor,
            );
          }
          syncStatus.value = "实时协作已连接";
        } else if (message.type === "ops" || message.type === "ops_replay") {
          updateOperationCursor(message.operation_cursor ?? message.cursor);
          updateCausalVector(message.state_vector);
          applyRemoteOperations(message.ops, message.version);
        } else if (message.type === "ops_ack") {
          updateOperationCursor(message.operation_cursor);
          updateCausalVector(message.state_vector);
          if (message.durable === false) {
            operationsInFlight = false;
            // The operation remains in the local replay queue. Do not create a
            // competing REST snapshot while the daemon is retrying its durable
            // append.
            syncStatus.value = "操作已接收 · 等待持久化";
          } else if (message.durable === true) {
            operationsInFlight = false;
            const rejected = Array.isArray(message.rejected)
              ? message.rejected
              : [];
            const pendingBefore = pendingOperations.slice();
            rememberConflicts(
              Array.isArray(message.conflicts) ? message.conflicts : [],
              rejected,
              pendingBefore,
              message.snapshot,
            );
            acknowledgeOperations(message.accepted || [], rejected);
            queue.clearPending();
            const deferred = Array.isArray(message.deferred) ? message.deferred : [];
            syncStatus.value = pendingOperations.length
              ? deferred.length
                ? `等待前置操作 · 待发送 ${pendingOperations.length}`
                : `实时协作已连接 · 待发送 ${pendingOperations.length}`
              : "实时协作已连接";
            if (!deferred.length) flushRealtimeOperations();
          }
          if (
            Array.isArray(message.rejected) &&
            message.rejected.length &&
            !syncConflicts.value.length
          ) {
            pendingNotice.value = `有 ${message.rejected.length} 个离线操作被服务器拒绝，请检查冲突后重试`;
          }
          if (
            message.snapshot &&
            Array.isArray(message.rejected) &&
            message.rejected.length
          )
            applyRemoteSnapshot(
              message.snapshot,
              message.version,
              message.operation_cursor,
            );
        } else if (message.type === "persisted") {
          if (typeof message.version === "number") version = message.version;
          updateOperationCursor(message.operation_cursor);
          updateCausalVector(message.state_vector);
          operationsInFlight = false;
          if (status.value === "已同步" || status.value === "待同步")
            status.value = "已保存";
          flushRealtimeOperations();
        } else if (message.type === "snapshot") {
          applyRemoteSnapshot(
            message.snapshot,
            message.version,
            message.operation_cursor,
          );
        } else if (message.type === "presence") {
          const person = message.collaborator;
          if (message.event === "left")
            collaborators.value = removeCollaborator(
              collaborators.value,
              person.id,
            );
          else if (person)
            collaborators.value = mergeCollaborator(
              collaborators.value,
              person,
            );
        } else if (message.type === "error") {
          syncStatus.value =
            message.code === "read_only"
              ? "只读协作"
              : `协作错误：${message.detail || "未知错误"}`;
        }
      } catch {
        syncStatus.value = "协作消息无效";
      }
    };
    socket.onerror = () => {
      syncStatus.value = "协作连接错误";
    };
    socket.onclose = () => {
      socket = undefined;
      operationsInFlight = false;
      if (ticketRefreshTimer) {
        clearTimeout(ticketRefreshTimer);
        ticketRefreshTimer = undefined;
      }
      // Keep the snapshot fallback paused while replayable operations are
      // pending; writing an old full snapshot here is the race this client
      // deliberately avoids. If there are no operations, REST can recover a
      // snapshot-only edit while the websocket reconnects.
      if (pendingOperations.length) {
        queue.pause();
      } else {
        queue.resume();
        if (canEdit.value && latestLocalSnapshot)
          queue.enqueue(latestLocalSnapshot);
      }
      if (disposed) return;
      syncStatus.value = "协作已断开 · 自动重连中";
      if (!reconnectTimer)
        reconnectTimer = setTimeout(() => {
          reconnectTimer = undefined;
          connectSync().catch(() => {});
        }, 3000);
    };
  } catch {
    if (pendingOperations.length) queue.pause();
    else queue.resume();
    // No role is granted until the signed ticket is accepted.  This keeps a
    // reader from briefly editing while the collaboration service is loading.
    if (!syncRole.value) syncStatus.value = "实时服务不可用 · 等待授权";
    else syncStatus.value = "实时服务不可用 · 使用版本保存";
    socket = undefined;
  }
}
function openEditor(k = "markdown") {
  if (!canEdit.value)
    return emit("error", "当前画布为只读，等待成员授权或切换到 editor/owner");
  kind.value = k;
  selected.value = "";
  text.value =
    k === "latex"
      ? "E = mc^2"
      : k === "mermaid"
        ? "graph TD\n A[能量来源] --> B[魔法体系]"
        : "# 一个新的想法\n\n从这里开始。";
  editing.value = true;
}
function editSelected() {
  if (!canEdit.value) return emit("error", "当前画布为只读");
  const shape = adapter.editor.getOnlySelectedShape() as Card | null;
  if (!shape || shape.type !== "oc-card" || shape.props.kind === "draft") {
    emit(
      "error",
      "请选择 Markdown、公式或图表卡片；实体草稿请在右侧审核面板编辑",
    );
    return;
  }
  selected.value = shape.id;
  kind.value = shape.props.kind;
  text.value = shape.props.text;
  editing.value = true;
}
function apply() {
  if (!canEdit.value) return emit("error", "当前画布为只读");
  if (selected.value)
    adapter.updateElement(selected.value, { text: text.value });
  else adapter.addCard(kind.value, text.value);
  editing.value = false;
}
function undo() {
  if (canEdit.value) adapter.undo();
}
function redo() {
  if (canEdit.value) adapter.redo();
}
function exportLocal() {
  const a = document.createElement("a");
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(adapter.getSnapshot(), null, 2)], {
      type: "application/json",
    }),
  );
  a.href = url;
  a.download = `${props.canvas.name}.json`;
  a.click();
  URL.revokeObjectURL(url);
}
async function recover() {
  if (!canEdit.value) return emit("error", "当前画布为只读");
  const raw = localStorage.getItem(key);
  if (!raw) return;
  const data = JSON.parse(raw);
  if (
    data.version !== version &&
    !confirm("本地副本基于旧版本。恢复后将替代当前画布，确认使用本地副本？")
  )
    return;
  adapter.loadSnapshot(JSON.parse(data.snapshot));
  queue.stopped = false;
  changed();
  await queue.flush();
}
const pointerMoveHandler = (event: PointerEvent) => {
  const rect = host.value?.getBoundingClientRect();
  if (rect)
    sendPresence({
      cursor: { x: event.clientX - rect.left, y: event.clientY - rect.top },
    });
};
const pointerLeaveHandler = () => sendPresence({ cursor: null });
function handleOffline() {
  if (disposed) return;
  syncStatus.value = "协作已断开 · 自动重连中";
  if (socket && socket.readyState !== WebSocket.CLOSED) socket.close();
}
function handleOnline() {
  if (disposed || socket?.readyState === WebSocket.OPEN || !syncRole.value)
    return;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = undefined;
  }
  syncStatus.value = "连接中…";
  connectSync().catch(() => {});
}
async function mountOfficialCanvas() {
  const ticket = await api<{
    ticket: string;
    url: string;
    role: "owner" | "editor" | "reader";
    branch: string;
    expires_at: number;
  }>(`canvases/${props.canvas.id}/sync-ticket/`, "POST", { client_id: clientId });
  officialSync = true;
  officialSyncActive.value = true;
  syncRole.value = ticket.role;
  setEditorReadOnly(ticket.role === "reader");
  const separator = ticket.url.includes("?") ? "&" : "?";
  const uri = `${ticket.url}${separator}ticket=${encodeURIComponent(ticket.ticket)}&protocol=tldraw-sync-v2&schema=oc-tldraw-2&branch=${encodeURIComponent(ticket.branch || "main")}`;
  root.render(React.createElement(OfficialCollaborativeCanvas, {
    uri,
    readOnly: ticket.role === "reader",
    licenseKey: import.meta.env.VITE_TLDRAW_LICENSE_KEY || undefined,
    onStatus: (value: string, error?: Error) => {
      syncStatus.value = value;
      if (error) emit("error", error.message);
    },
    onMount(editor: Editor) {
      // 官方 TLDraw sync-v2 负责冲突收敛；清理 legacy records-v1 的
      // 本地提示，避免旧页面状态在切换协议后继续显示“协作冲突”。
      syncConflicts.value = [];
      pendingNotice.value = "";
      adapter = new TldrawAdapter(editor);
      setEditorReadOnly(ticket.role === "reader");
      setProposals(props.proposals);
      dispose = editor.store.listen((entry) => {
        if (entry.source === "user") changed(entry.changes as RecordsDiff);
      }, { scope: "document", source: "user" });
      props.proposals.forEach((p) => adapter.createDraftElement(p));
      clearLegacyQueueAfterOfficialHandshake();
      syncConflicts.value = [];
      pendingNotice.value = "";
      syncStatus.value = "官方 CRDT 已连接";
      status.value = "实时协作已连接";
    },
  }));
}

onMounted(() => {
  root = createRoot(host.value!);
  host.value?.addEventListener("pointermove", pointerMoveHandler);
  host.value?.addEventListener("pointerleave", pointerLeaveHandler);
  if (import.meta.env.VITE_TLDRAW_OFFICIAL_SYNC === "1") {
    mountOfficialCanvas().catch((error) => {
      officialSync = false;
      officialSyncActive.value = false;
      syncStatus.value = "官方 CRDT 不可用";
      emit("error", error instanceof Error ? error.message : "官方 CRDT 连接失败");
    });
    return;
  }
  root.render(
    React.createElement(Tldraw, {
      shapeUtils: [CardUtil],
      licenseKey: import.meta.env.VITE_TLDRAW_LICENSE_KEY || undefined,
      onMount(editor: Editor) {
        adapter = new TldrawAdapter(editor);
        setEditorReadOnly(true);
        try {
          adapter.loadSnapshot(props.canvas.snapshot);
        } catch {
          status.value = "快照加载失败";
          emit("error", "无法解析快照，已停止自动保存以保护原始数据");
          queue.stopped = true;
          return;
        }
        setProposals(props.proposals);
        dispose = editor.store.listen(
          (entry) => {
            if (entry.source === "user") changed(entry.changes as RecordsDiff);
          },
          { scope: "document", source: "user" },
        );
        props.proposals.forEach((p) => adapter.createDraftElement(p));
        if (localStorage.getItem(key)) status.value = "发现本地副本 · 可恢复";
        connectSync().catch(() => {});
      },
    }),
  );
});
watch(
  () => props.proposals,
  (proposals) => {
    setProposals(proposals);
    if (adapter) proposals.forEach((p) => adapter.createDraftElement(p));
  },
  { deep: true },
);
function beforeUnload(e: BeforeUnloadEvent) {
  if (status.value !== "已保存") {
    e.preventDefault();
    e.returnValue = "";
  }
}
window.addEventListener("beforeunload", beforeUnload);
window.addEventListener("offline", handleOffline);
window.addEventListener("online", handleOnline);
onBeforeUnmount(() => {
  disposed = true;
  if (presenceTimer) clearTimeout(presenceTimer);
  if (reconnectTimer) clearTimeout(reconnectTimer);
  if (ticketRefreshTimer) clearTimeout(ticketRefreshTimer);
  socket?.close();
  host.value?.removeEventListener("pointermove", pointerMoveHandler);
  host.value?.removeEventListener("pointerleave", pointerLeaveHandler);
  queue.dispose();
  dispose?.();
  root?.unmount();
  window.removeEventListener("beforeunload", beforeUnload);
  window.removeEventListener("offline", handleOffline);
  window.removeEventListener("online", handleOnline);
});
defineExpose({ flush: () => queue.flush() });
</script>
<template>
  <section class="canvas-surface">
    <div class="canvas-tools">
      <span class="eyebrow">STAGING · 自由创作，不急于定稿</span>
      <div class="tool-row">
        <button :disabled="isReadOnly" @click="openEditor()">＋ Markdown</button
        ><button :disabled="isReadOnly" @click="openEditor('latex')">
          ƒ 公式</button
        ><button :disabled="isReadOnly" @click="openEditor('mermaid')">
          ◇ 图表 / 思维导图</button
        ><button :disabled="isReadOnly" @click="editSelected">编辑卡片</button
        ><button :disabled="isReadOnly" @click="undo" aria-label="撤销">
          ↶</button
        ><button :disabled="isReadOnly" @click="redo" aria-label="重做">
          ↷
        </button>
      </div>
    </div>
    <div class="canvas-stage">
      <div
        ref="host"
        class="tldraw-host"
        :class="{ 'sync-readonly': isReadOnly }"
      />
      <div class="presence-layer" aria-hidden="true">
        <div
          v-for="collaborator in collaborators.filter((c) => c.id && c.cursor)"
          :key="collaborator.id"
          class="remote-cursor"
          :style="{
            left: `${collaborator.cursor!.x}px`,
            top: `${collaborator.cursor!.y}px`,
            '--presence-color': presenceColor(collaborator.id),
          }"
        >
          <span class="remote-cursor-dot" /><span class="remote-cursor-label"
            >{{ presenceName(collaborator)
            }}<small v-if="collaborator.selection?.length">
              · {{ collaborator.selection.length }} 个选中</small
            ></span
          >
        </div>
      </div>
    </div>
    <footer class="canvas-status">
      <span>● {{ displayedCanvasStatus }} · v{{ version }}</span>
      ><span
        >◉ {{ syncStatus
        }}<template v-if="pendingCount"> · 待发送 {{ pendingCount }}</template
        ><template v-if="collaborators.length">
          · {{ collaborators.length }} 位协作者</template
        ><template v-if="syncRole === 'reader'"> · 只读</template></span
      ><span v-if="pendingNotice" class="canvas-warning"
        >⚠ {{ pendingNotice }}</span
      ><span>便签 / 手绘 / 箭头使用画布工具栏</span
      ><button
        :disabled="isReadOnly"
        @click="queue.retry().catch((e) => emit('error', String(e)))"
      >
        重试保存</button
      ><button
        :disabled="isReadOnly"
        @click="recover().catch((e) => emit('error', String(e)))"
      >
        恢复本地副本</button
      ><button @click="exportLocal">导出快照</button>
    </footer>
    <section
      v-if="syncConflicts.length && !officialSync"
      class="sync-conflict-panel"
      aria-label="协作冲突审核"
    >
      <header>
        <strong>协作冲突 · {{ syncConflicts.length }}</strong
        ><small>系统已按确定性版本保留远端结果；你可以逐字段重试本地值。</small>
      </header>
      <article
        v-for="conflict in syncConflicts"
        :key="conflict.key"
        class="sync-conflict-card"
      >
        <div class="sync-conflict-title">
          <strong>{{ conflict.recordId }}</strong
          ><span>{{ conflict.reason }}</span>
        </div>
        <div
          v-for="field in conflict.fields"
          :key="field"
          class="sync-conflict-field"
        >
          <code>{{ field === "__record__" ? "整条记录" : field }}</code
          ><span class="sync-conflict-kind"
            ><small>{{ conflictCategoryLabel(conflict.fieldCategories[field]) }}</small
            ><small>{{ conflictStrategyLabel(conflict.mergeStrategies[field]) }}</small
          ></span
          ><span
            ><small>本地</small
            >{{
              formatConflictValue(conflictLocalValue(conflict, field))
            }}</span
          ><span
            ><small>远端</small
            >{{
              formatConflictValue(conflictRemoteValue(conflict, field))
            }}</span
          >
        </div>
        <div class="sync-conflict-actions">
          <button @click="dismissConflict(conflict.key)">保留远端</button
          ><button
            class="primary"
            :disabled="isReadOnly"
            @click="retryConflict(conflict)"
          >
            重试本地值
          </button>
        </div>
      </article>
    </section>
    <div v-if="editing" class="modal-backdrop">
      <section class="modal">
        <h2>编辑 {{ kind }}</h2>
        <textarea v-model="text" rows="12" aria-label="卡片内容" />
        <div class="actions">
          <button @click="editing = false">取消</button
          ><button class="primary" @click="apply">放入画布</button>
        </div>
      </section>
    </div>
  </section>
</template>
