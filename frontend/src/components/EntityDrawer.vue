<script setup lang="ts">
import { ref, watch } from 'vue'
import { api } from '../services/api'
import { entityTypes, type Entity } from '../types'
const props = defineProps<{ id: string; workspace: string; branch: string }>()
const emit = defineEmits<{ close: []; error: [message: string]; select: [id: string] }>()
const entity = ref<Entity>()
const links = ref<{ outgoing: any[]; incoming: any[] }>()
const loading = ref(false)
let generation = 0
watch(() => [props.id, props.workspace, props.branch], async () => {
  const current = ++generation
  loading.value = true; entity.value = undefined; links.value = undefined
  const params = new URLSearchParams({ workspace: props.workspace })
  if (props.branch !== 'main') params.set('branch', props.branch)
  try {
    const [record, relations] = await Promise.all([api<Entity>(`entities/${props.id}/?${params}`), api<typeof links.value>(`entities/${props.id}/links/?${params}`)])
    if (current === generation) { entity.value = record; links.value = relations }
  } catch (error) { if (current === generation) emit('error', String(error)) }
  finally { if (current === generation) loading.value = false }
}, { immediate: true })
</script>
<template>
  <aside class="entity-drawer" aria-label="实体详情" @keydown.esc="emit('close')">
    <header><strong>实体详情</strong><button autofocus aria-label="关闭实体详情" @click="emit('close')">关闭 ×</button></header>
    <p v-if="loading" role="status">正在读取实体…</p>
    <template v-if="entity">
      <small>{{ entityTypes[entity.type] }} · {{ entity.status === 'active' ? '有效' : '已归档' }}</small>
      <h2>{{ entity.title }}</h2><p class="entity-content">{{ entity.content }}</p>
      <h3>关联关系</h3>
      <button v-for="link in links?.outgoing" :key="link.id || link.otherEntity.id" @click="emit('select', link.otherEntity.id)">{{ link.relationLabel }} → {{ link.otherEntity.title }}</button>
      <button v-for="link in links?.incoming" :key="link.id || link.otherEntity.id" @click="emit('select', link.otherEntity.id)">{{ link.otherEntity.title }} → {{ link.relationLabel }}</button>
      <p v-if="!links?.outgoing.length && !links?.incoming.length">暂无关联关系</p>
      <h3>版本与持久化</h3><p>{{ entity.sync_status }} · {{ entity.commit_hash || '尚未写入 Git' }}</p><small>{{ entity.git_path }}</small>
    </template>
  </aside>
</template>
<style scoped>
.entity-drawer { position:fixed; right:0; top:0; bottom:0; width:min(420px, 100vw); z-index:90; background:#fafbf8; border-left:1px solid #b7c5bb; padding:24px; overflow:auto; box-shadow:-8px 0 30px #25372b18; }
header { display:flex; align-items:center; justify-content:space-between; }
.entity-content { white-space:pre-wrap; overflow-wrap:anywhere; }
.entity-drawer > button { display:block; width:100%; margin:6px 0; text-align:left; }
</style>
