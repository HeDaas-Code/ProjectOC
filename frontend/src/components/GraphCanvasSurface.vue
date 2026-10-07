<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createRoot, type Root } from 'react-dom/client'
import React from 'react'
import { react, type Editor } from '@tldraw/tldraw'
import { OfficialCollaborativeCanvas } from '../canvas/OfficialCollaborativeCanvas'
import { syncGraphProjection } from '../canvas/adapter'
import { api } from '../services/api'
import type { Canvas } from '../types'

type Node = { id: string; title: string; type: string; status?: string }
type Edge = { id: string; source: string; target: string; label: string }
const props = defineProps<{ canvas: Canvas; nodes: Node[]; edges: Edge[] }>()
const emit = defineEmits<{ error: [message: string]; select: [id: string]; relation: [id: string] }>()
const host = ref<HTMLElement>(); const status = ref('连接中…'); const role = ref('reader'); let root: Root; let disposed = false; let activeEditor: Editor | undefined; let stopSelection: (() => void) | undefined
onMounted(async () => {
  root = createRoot(host.value!)
  try {
    const clientId = crypto.randomUUID()
    const ticket = await api<{ ticket: string; url: string; role: string; branch: string }>(`canvases/${props.canvas.id}/sync-ticket/`, 'POST', { client_id: clientId })
    role.value = ticket.role
    let first: typeof ticket | undefined = ticket
    const uri = async () => { const current = first || await api<typeof ticket>(`canvases/${props.canvas.id}/sync-ticket/`, 'POST', { client_id: clientId }); first = undefined; return `${current.url}?ticket=${encodeURIComponent(current.ticket)}&protocol=tldraw-sync-v2&schema=oc-tldraw-2&branch=${encodeURIComponent(current.branch)}` }
    root.render(React.createElement(OfficialCollaborativeCanvas, { uri, readOnly: ticket.role === 'reader', licenseKey: import.meta.env.VITE_TLDRAW_LICENSE_KEY || undefined, onStatus: (value: string, error?: Error) => { status.value = value; if (error) emit('error', error.message) }, onMount: (editor: Editor) => { if (disposed) return; activeEditor = editor; syncGraphProjection(editor, props.nodes, props.edges); stopSelection = react('graph selection', () => { const shape = editor.getSelectedShapes()[0]; if (shape?.type === 'oc-entity') emit('select', shape.props.entityId); else if (shape?.type === 'arrow' && String(shape.id).includes('relation:')) emit('relation', String(shape.id).split('relation:')[1]) }) } }))
  } catch (error) { status.value = '实时协作连接失败'; emit('error', String(error)) }
})
function autoLayout() {
  if (!activeEditor || role.value === 'reader') return
  const shapes = activeEditor.getCurrentPageShapes().filter(s => s.type === 'oc-entity').sort((a, b) => a.props.entityType.localeCompare(b.props.entityType) || a.props.title.localeCompare(b.props.title))
  activeEditor.updateShapes(shapes.map((s, i) => ({ id: s.id, type: s.type, x: 120 + i % 4 * 280, y: 120 + Math.floor(i / 4) * 150 })))
}
watch(() => [props.nodes, props.edges], () => { if (activeEditor) syncGraphProjection(activeEditor, props.nodes, props.edges) })
onBeforeUnmount(() => { disposed = true; stopSelection?.(); root?.unmount() })
</script>
<template>
  <section class="graph-canvas-surface"><div class="graph-canvas-toolbar"><span class="eyebrow">WORLD GRAPH · 正式设定编排</span><button v-if="role !== 'reader'" @click="autoLayout">按类型自动布局</button><span class="muted">{{ nodes.length }} 个节点 · {{ edges.length }} 条关系 · {{ status }}<template v-if="role === 'reader'"> · 只读</template></span></div><div ref="host" class="graph-canvas-host" /></section>
</template>
