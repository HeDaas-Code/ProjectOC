<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../services/api'
import { useWorkbench } from '../stores/workbench'
import MaintenanceStatusPanel from './MaintenanceStatusPanel.vue'
const props = defineProps<{ branch?: string; canvas?: string; canEdit?: boolean }>()
const emit = defineEmits<{ 'focus-target': [id: string] }>()
const store = useWorkbench(), history = ref<{ hash: string; date: string; message: string }[]>([]), diff = ref(''), temporal = ref<any>(), temporalSnapshot = ref<any>()
const temporalRows = computed(() => Object.entries(temporal.value?.changes || {}).map(([kind, change]: [string, any]) => ({ kind, change })))
function temporalLabel(kind: string) { return ({ time_systems: '时间体系', conversions: '时间换算', timeline_entries: '时间线事件', character_lifespans: '人物生命周期' } as Record<string, string>)[kind] || kind }
function temporalItemLabel(kind: string, item: any) {
  if (kind === 'timeline_entries') return item.summary || `事件 ${String(item.event || item.id).slice(0, 8)}`
  if (kind === 'character_lifespans') return `人物 ${String(item.character || item.id).slice(0, 8)}`
  if (kind === 'time_systems') return item.name || `时间体系 ${String(item.id).slice(0, 8)}`
  if (kind === 'conversions') return `${String(item.source_system).slice(0, 8)} → ${String(item.target_system).slice(0, 8)}`
  return String(item.id).slice(0, 8)
}
function changedFields(item: any) {
  const before = item.before || {}, after = item.after || {}
  return Object.keys({ ...before, ...after }).filter(key => key !== 'id' && JSON.stringify(before[key]) !== JSON.stringify(after[key])).slice(0, 5)
}
async function load() { try { const branch = props.branch && props.branch !== 'main' ? `?branch=${encodeURIComponent(props.branch)}` : ''
 const data = await api(`workspaces/${store.workspace!.id}/git/history/${branch}`); history.value = data.history; await store.refreshWorld() } catch(e) { store.error = String(e) } }
async function show(hash: string) { try { const branch = props.branch && props.branch !== 'main' ? `&branch=${encodeURIComponent(props.branch)}` : ''; diff.value = (await api(`workspaces/${store.workspace!.id}/git/diff/?from=${hash}${branch}`)).diff; temporal.value = (await api(`workspaces/${store.workspace!.id}/git/temporal/?from=${hash}${branch}`)).diff; temporalSnapshot.value = (await api(`workspaces/${store.workspace!.id}/git/temporal/?snapshot=1&commit=${hash}${branch}`)).snapshot } catch(e) { store.error = String(e) } }
async function retry(id: string) { try { await api(`commit-jobs/${id}/retry/`, 'POST', {}); await load() } catch(e) { store.error = String(e) } }
async function proposalCreated() {
  try { await store.refreshProposals() } catch (e) { store.error = String(e) }
}
onMounted(load)
watch(() => props.branch, load)
</script>
<template><section class="history-panel"><MaintenanceStatusPanel v-if="store.workspace" :workspace="store.workspace.id" :branch="props.branch" :canvas="props.canvas" :can-edit="props.canEdit" @error="store.error = $event" @focus-target="emit('focus-target', $event)" @proposal-created="proposalCreated"/><span class="eyebrow">MAINTENANCE PHILOSOPHY</span><h2>世界的修订记录</h2><p>数据库保存已确认事实，Git 留下每次选择的轨迹。</p><div v-for="job in store.jobs" class="job"><code>{{ job.id.slice(0,8) }}</code> <span>{{ job.status }}</span><button v-if="job.status !== 'synced'" @click="retry(job.id)">重试同步</button><p v-if="job.error_message" class="warning">{{ job.error_message }}</p></div><div class="history-layout"><div><button class="commit-entry" v-for="h in history" :key="h.hash" @click="show(h.hash)"><code>{{ h.hash.slice(0,8) }}</code><strong>{{ h.message }}</strong><small>{{ h.date }}</small></button><p v-if="!history.length">尚无提交记录</p></div><div class="history-detail"><section class="temporal-diff" v-if="temporal"><h3>时间线结构变化</h3><p class="muted">{{ temporal.from ? temporal.from.slice(0,8) : '空' }} → {{ temporal.to ? temporal.to.slice(0,8) : '空' }}</p><div v-for="row in temporalRows" :key="row.kind" class="temporal-diff-row"><div class="temporal-diff-heading"><strong>{{ temporalLabel(row.kind) }}</strong><span>新增 {{ row.change.added.length }} · 修改 {{ row.change.changed.length }} · 移除 {{ row.change.removed.length }}</span></div><div v-if="row.change.added.length || row.change.changed.length || row.change.removed.length" class="temporal-change-groups"><div v-if="row.change.added.length" class="temporal-change-group added"><b>＋ 新增</b><span v-for="item in row.change.added.slice(0, 6)" :key="`a-${item.id}`">{{ temporalItemLabel(row.kind, item) }}</span></div><div v-if="row.change.changed.length" class="temporal-change-group changed"><b>↔ 修改</b><span v-for="item in row.change.changed.slice(0, 6)" :key="`c-${item.after?.id || item.before?.id}`">{{ temporalItemLabel(row.kind, item.after) }}（{{ changedFields(item).join('、') || '内容' }}）</span></div><div v-if="row.change.removed.length" class="temporal-change-group removed"><b>− 移除</b><span v-for="item in row.change.removed.slice(0, 6)" :key="`r-${item.id}`">{{ temporalItemLabel(row.kind, item) }}</span></div><small v-if="row.change.added.length + row.change.changed.length + row.change.removed.length > 18" class="muted">仅显示每类前 6 项，完整内容仍可通过原始 diff 查看。</small></div></div></section><section class="temporal-snapshot" v-if="temporalSnapshot"><h3>该版本的时间状态</h3><p class="muted">提交 {{ temporalSnapshot.commit.slice(0,8) }} · {{ temporalSnapshot.time_systems.length }} 个时间体系 · {{ temporalSnapshot.timeline_entries.length }} 个事件 · {{ temporalSnapshot.character_lifespans.length }} 个人物生命周期</p><div v-for="entry in temporalSnapshot.timeline_entries.slice(0,8)" :key="entry.id" class="snapshot-entry"><strong>{{ entry.summary || entry.event }}</strong><span>坐标 {{ entry.start_value }}{{ entry.end_value !== null ? ` – ${entry.end_value}` : '' }}</span></div><p v-if="temporalSnapshot.timeline_entries.length > 8" class="muted">还有 {{ temporalSnapshot.timeline_entries.length - 8 }} 个事件未展开。</p></section><pre>{{ diff || '选择一个提交查看 diff' }}</pre></div></div></section></template>
