<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../services/api'
import type { CanvasContainer } from '../types'
const props = defineProps<{ container: CanvasContainer }>()
const emit = defineEmits<{ open: [canvas: string]; select: [entity: string]; error: [message: string] }>()
const expanded = ref(false)
const projection = ref<{ children: CanvasContainer[]; nodes: { id: string; entity_id: string | null; title: string; content: string; status: string }[]; edges: { id: string; source_title: string; target_title: string; relation_type: string }[] }>()
let generation = 0
let timer: ReturnType<typeof setInterval> | undefined
async function load() {
  const current = generation
  try { const result = await api<typeof projection.value>(`canvas-containers/${props.container.id}/projection/`); if (current === generation && expanded.value) projection.value = result }
  catch (error) { if (current === generation) emit('error', String(error)) }
}
watch(expanded, value => { generation++; if (timer) clearInterval(timer); if (value) { void load(); timer = setInterval(load, 10000) } else projection.value = undefined })
onBeforeUnmount(() => { generation++; if (timer) clearInterval(timer) })
</script>
<template>
  <article class="container-projection">
    <header @dblclick="emit('open', container.canvas)"><button :aria-expanded="expanded" @click="expanded = !expanded">{{ expanded ? '▾' : '▸' }} {{ container.name }}</button><button @click="emit('open', container.canvas)">进入编辑 →</button></header>
    <section v-if="expanded" aria-label="子画布只读展开">
      <small>只读展开 · 进入子画布编辑</small>
      <p v-if="!projection">正在读取子画布…</p>
      <template v-else>
        <div class="projection-nodes"><button v-for="node in projection.nodes" :key="node.id" @click="node.entity_id && emit('select', node.entity_id)"><strong>{{ node.title }}</strong><small>{{ node.status === 'accepted' ? '正式' : '草稿' }}</small><p>{{ node.content }}</p></button></div>
        <p v-for="edge in projection.edges" :key="edge.id">{{ edge.source_title }} → {{ edge.target_title }} · {{ edge.relation_type }}</p>
        <ContainerProjection v-for="child in projection.children" :key="child.id" :container="child" @open="emit('open', $event)" @select="emit('select', $event)" @error="emit('error', $event)" />
      </template>
    </section>
  </article>
</template>
<style scoped>
.container-projection { border: 1px solid #b7c5bb; border-radius: 12px; padding: 12px; margin: 8px; background: #f8faf7; }
header { display:flex; justify-content:space-between; gap:8px; }
.projection-nodes { display:flex; flex-wrap:wrap; gap:8px; }
.projection-nodes button { width:220px; text-align:left; padding:12px; }
.projection-nodes p { max-height:100px; overflow:auto; white-space:pre-wrap; }
</style>
