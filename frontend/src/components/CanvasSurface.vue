<script setup lang="ts">
import StyledDialog from './StyledDialog.vue'
import { computed, onMounted, onBeforeUnmount, ref, watch } from "vue";
import { createRoot, type Root } from "react-dom/client";
import React from "react";
import type { Editor } from "@tldraw/tldraw";
import { OfficialCollaborativeCanvas } from "../canvas/OfficialCollaborativeCanvas";
import "@tldraw/tldraw/tldraw.css";
import "katex/dist/katex.min.css";
import { TldrawAdapter, setProposals, type Card } from "../canvas/adapter";
import type { Canvas, Proposal } from "../types";
import { api } from "../services/api";
const props = defineProps<{ canvas: Canvas; proposals: Proposal[] }>();
const emit = defineEmits<{ saved: [canvas: Canvas]; error: [message: string] }>();
const host = ref<HTMLElement>(), editing = ref(false), text = ref(""), kind = ref("markdown"), selected = ref("");
const syncStatus = ref("连接中…"), syncRole = ref<"owner" | "editor" | "reader">();
const ready = ref(false);
const canEdit = computed(() => ready.value && (syncRole.value === "owner" || syncRole.value === "editor"));
const isReadOnly = computed(() => !canEdit.value);
let root: Root, adapter: TldrawAdapter | undefined, dispose: (() => void) | undefined, disposed = false;
const key = `oc:official-draft:${props.canvas.id}`;
const legacySnapshot = localStorage.getItem(`oc:draft:${props.canvas.id}`);
const legacyKey = `oc:sync-pending-ops:${props.canvas.id}`;
const legacyBackup = localStorage.getItem(legacyKey) || localStorage.getItem(`oc:legacy-pending-ops-backup:${props.canvas.id}`);
const hasLegacyPending = computed(() => Boolean(legacyBackup && legacyBackup !== '[]'));
function download(value: string, filename: string) {
  const url = URL.createObjectURL(new Blob([value], { type: 'application/json' }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename; anchor.click(); URL.revokeObjectURL(url);
}
function exportLegacySnapshot() { if (legacySnapshot) download(legacySnapshot, `${props.canvas.name}-legacy-snapshot.json`); }
function exportLegacyPending() { if (legacyBackup) download(legacyBackup, `${props.canvas.name}-legacy-operations.json`); }
function changed() {
  if (!adapter || !canEdit.value) return;
  try { localStorage.setItem(key, JSON.stringify({ version: props.canvas.snapshot_version, snapshot: JSON.stringify(adapter.getSnapshot()) })); }
  catch { emit('error', '本地存储空间不足，请导出画布'); }
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
  const shape = adapter!.editor.getOnlySelectedShape() as Card | null;
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
    adapter!.updateElement(selected.value, { text: text.value });
  else adapter!.addCard(kind.value, text.value);
  editing.value = false;
}
function undo() {
  if (canEdit.value) adapter!.undo();
}
function redo() {
  if (canEdit.value) adapter!.redo();
}
function arrangeGrid() {
  if (!canEdit.value) return emit("error", "当前画布为只读");
  adapter!.arrangeGrid();
}
function exportLocal() {
  const a = document.createElement("a");
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(adapter!.getSnapshot(), null, 2)], {
      type: "application/json",
    }),
  );
  a.href = url;
  a.download = `${props.canvas.name}.json`;
  a.click();
  URL.revokeObjectURL(url);
}
onMounted(async () => {
  window.addEventListener('oc:arrange-grid', arrangeGrid);
  root = createRoot(host.value!);
  try {
    const clientId = crypto.randomUUID();
    const requestTicket = () => api<{ ticket: string; url: string; role: "owner" | "editor" | "reader"; branch: string }>(`canvases/${props.canvas.id}/sync-ticket/`, "POST", { client_id: clientId });
    const ticket = await requestTicket();
    if (disposed) return;
    syncRole.value = ticket.role;
    let firstTicket: typeof ticket | undefined = ticket;
    const uri = async () => {
      const current = firstTicket || await requestTicket(); firstTicket = undefined;
      if (disposed) throw new Error('画布已关闭');
      syncRole.value = current.role;
      adapter?.editor.updateInstanceState({ isReadonly: current.role === 'reader' });
      const separator = current.url.includes('?') ? '&' : '?';
      return `${current.url}${separator}ticket=${encodeURIComponent(current.ticket)}&protocol=tldraw-sync-v2&schema=oc-tldraw-2&branch=${encodeURIComponent(current.branch || 'main')}`;
    };
    root.render(React.createElement(OfficialCollaborativeCanvas, {
      uri,
      readOnly: ticket.role === 'reader', licenseKey: import.meta.env.VITE_TLDRAW_LICENSE_KEY || undefined,
      onStatus: (value: string, error?: Error) => { if (disposed) return; syncStatus.value = value; if (error) emit('error', error.message); },
      onMount: (editor: Editor) => {
        dispose?.(); adapter = new TldrawAdapter(editor); ready.value = true;
        editor.updateInstanceState({ isReadonly: ticket.role === 'reader' });
        setProposals(props.proposals);
        dispose = editor.store.listen(() => changed(), { scope: 'document', source: 'user' });
        if (ticket.role !== 'reader') props.proposals.forEach(p => adapter!.createDraftElement(p));
      },
    }));
  } catch (error) { syncRole.value = undefined; syncStatus.value = '实时协作连接失败'; emit('error', String(error)); }
});
watch(() => props.proposals, proposals => { setProposals(proposals); if (canEdit.value) proposals.forEach(p => adapter!.createDraftElement(p)); }, { deep: true });
onBeforeUnmount(() => { disposed = true; window.removeEventListener('oc:arrange-grid', arrangeGrid); dispose?.(); root?.unmount(); });
// Official useSync owns transport; switching tabs must never trigger a REST snapshot write.
defineExpose({ flush: async () => {
  if (ready.value && syncStatus.value !== '实时协作已连接') throw new Error('画布尚未连接，请等待重连或导出快照后再切换');
} });
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
        ><button :disabled="isReadOnly" @click="arrangeGrid">整理布局</button
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
    </div>
    <footer class="canvas-status">
      <span>● {{ syncStatus }}<template v-if="syncRole === 'reader'"> · 只读</template></span>
      <span>便签 / 手绘 / 箭头使用画布工具栏</span>
      <span v-if="hasLegacyPending" class="canvas-warning">发现旧版未同步操作，请导出备份后人工核对</span>
      <button v-if="hasLegacyPending" @click="exportLegacyPending">导出旧版操作</button>
      <button v-if="legacySnapshot" @click="exportLegacySnapshot">导出旧版本地快照</button>
      <button :disabled="!ready" @click="exportLocal">导出快照</button>
    </footer>
    <StyledDialog :open="editing" :title="`编辑 ${kind}`" @close="editing = false">
        <textarea v-model="text" rows="12" aria-label="卡片内容" />
        <div class="actions">
          <button @click="editing = false">取消</button
          ><button class="primary" @click="apply">放入画布</button>
        </div>
    </StyledDialog>
  </section>
</template>
