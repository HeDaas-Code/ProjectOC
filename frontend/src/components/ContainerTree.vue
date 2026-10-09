<script setup lang="ts">
import { computed, ref } from 'vue'
import type { CanvasContainer } from '../types'
const props = defineProps<{ containers: CanvasContainer[]; parent?: string | null; active?: string; canEdit: boolean }>()
const emit = defineEmits<{ open: [canvas: string]; create: [parent: string] }>()
const expanded = ref<string[]>([])
const children = computed(() => props.containers.filter(c => c.parent === (props.parent || null) && c.status !== 'archived'))
function toggle(id: string) { expanded.value = expanded.value.includes(id) ? expanded.value.filter(x => x !== id) : [...expanded.value, id] }
</script>
<template>
  <ul class="container-tree">
    <li v-for="container in children" :key="container.id">
      <div class="container-tree-row">
        <button :aria-expanded="expanded.includes(container.id)" :aria-label="`展开 ${container.name}`" @click="toggle(container.id)">{{ expanded.includes(container.id) ? '▾' : '▸' }}</button>
        <button :class="{ selected: active === container.canvas }" @click="emit('open', container.canvas)">▣ {{ container.name }}</button>
        <button v-if="canEdit" :aria-label="`在 ${container.name} 下新建设定系`" @click="emit('create', container.id)">＋</button>
      </div>
      <ContainerTree v-if="expanded.includes(container.id)" :containers="containers" :parent="container.id" :active="active" :can-edit="canEdit" @open="emit('open', $event)" @create="emit('create', $event)" />
    </li>
  </ul>
</template>
<style scoped>
.container-tree { list-style: none; padding: 0 0 0 8px; margin: 0; }
.container-tree-row { display: flex; align-items: center; gap: 2px; }
.container-tree-row button { padding: 6px 4px; border: 0; background: transparent; text-align: left; }
.container-tree-row button:nth-child(2) { flex: 1; overflow: hidden; text-overflow: ellipsis; }
.container-tree-row .selected { background: #e1ebe3; }
</style>
