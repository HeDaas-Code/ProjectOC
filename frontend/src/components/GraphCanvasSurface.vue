<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { createRoot, type Root } from 'react-dom/client'
import React from 'react'
import type { Editor } from '@tldraw/tldraw'
import { OfficialCollaborativeCanvas } from '../canvas/OfficialCollaborativeCanvas'
import { syncGraphProjection } from '../canvas/adapter'
import { api } from '../services/api'
import type { Canvas } from '../types'

type Node = { id: string; title: string; type: string; status?: string }
type Edge = { id: string; source: string; target: string; label: string }
const props = defineProps<{ canvas: Canvas; nodes: Node[]; edges: Edge[] }>()
const emit = defineEmits<{ error: [message: string] }>()
const host = ref<HTMLElement>(); const status = ref('连接中…'); const role = ref('reader'); let root: Root; let disposed = false
onMounted(async () => {
  root = createRoot(host.value!)
  try {
    const clientId = crypto.randomUUID()
    const ticket = await api<{ ticket: string; url: string; role: string; branch: string }>(`canvases/${props.canvas.id}/sync-ticket/`, 'POST', { client_id: clientId })
    role.value = ticket.role
    let first: typeof ticket | undefined = ticket
    const uri = async () => { const current = first || await api<typeof ticket>(`canvases/${props.canvas.id}/sync-ticket/`, 'POST', { client_id: clientId }); first = undefined; return `${current.url}?ticket=${encodeURIComponent(current.ticket)}&protocol=tldraw-sync-v2&schema=oc-tldraw-2&branch=${encodeURIComponent(current.branch)}` }
    root.render(React.createElement(OfficialCollaborativeCanvas, { uri, readOnly: ticket.role === 'reader', licenseKey: import.meta.env.VITE_TLDRAW_LICENSE_KEY || undefined, onStatus: (value: string, error?: Error) => { status.value = value; if (error) emit('error', error.message) }, onMount: (editor: Editor) => { if (!disposed) syncGraphProjection(editor, props.nodes, props.edges) } }))
  } catch (error) { status.value = '实时协作连接失败'; emit('error', String(error)) }
})
onBeforeUnmount(() => { disposed = true; root?.unmount() })
</script>
<template>
  <section class="graph-canvas-surface"><div class="graph-canvas-toolbar"><span class="eyebrow">WORLD GRAPH · 正式设定编排</span><span class="muted">{{ nodes.length }} 个节点 · {{ edges.length }} 条关系 · {{ status }}<template v-if="role === 'reader'"> · 只读</template></span></div><div ref="host" class="graph-canvas-host" /></section>
</template>
