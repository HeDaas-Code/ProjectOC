<script setup lang="ts">
import { requestDialog, confirmAction } from '../services/dialog'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import cytoscape, { type Core } from 'cytoscape'
import { api } from '../services/api'
import { formatWorldTime } from '../services/timeFormat'
import type { Entity, TimelineDiff } from '../types'
import TimeSystemEditor from './TimeSystemEditor.vue'

const props = defineProps<{ workspace: string; branch: string; entities: Entity[] }>()
const emit = defineEmits<{ error: [message: string]; 'focus-target': [target: { id: string; kind: string; sourceId?: string }] }>()

type TimeSystem = { id: string; name: string; epoch_label: string; unit_name: string; units: { name: string; ticks: number }[]; calendar_rules?: Record<string, unknown>; display_format?: string }
type Lifespan = { id: string; character: string; character_title: string; time_system: string; start_value: number | null; end_value: number | null; archived: boolean }
type TimelineEntry = { id: string; timeline: string; timeline_title: string; event: string; event_title: string; start_value: number; end_value: number | null; summary: string; sequence: number; participant_details: { id: string; title: string; role: string }[] }
type GitCommit = { hash: string; date: string; message: string }
type HistoricalEntity = { id: string; type: string; title: string; status: string; content?: string; metadata?: Record<string, unknown> }
type TemporalSnapshot = { commit: string; entities: HistoricalEntity[]; relations: unknown[]; time_systems: TimeSystem[]; conversions: unknown[]; timeline_entries: Array<{ id: string; timeline: string; event: string; time_system: string; start_value: number; end_value: number | null; sequence: number; summary: string; archived?: boolean; participants?: { entity: string; role: string }[] }>; character_lifespans: Array<{ id: string; character: string; time_system: string; start_value: number | null; end_value: number | null; archived?: boolean }> }

const systems = ref<TimeSystem[]>([])
const selectedSystem = ref('')
const at = ref(0)
const timelineId = ref('')
const entries = ref<TimelineEntry[]>([])
const lifespans = ref<Lifespan[]>([])
const slice = ref<any>()
const host = ref<HTMLElement>()
const rangeStart = ref(0)
const rangeEnd = ref(100)
const dataMin = ref(0)
const dataMax = ref(100)
const charId = ref('')
const birth = ref(0)
const death = ref<number>()
const timelineEntity = ref('')
const eventEntity = ref('')
const eventAt = ref(0)
const eventEnd = ref<number>()
const participants = ref<string[]>([])
const participantRoles = ref<Record<string, string>>({})
const selectedEventId = ref('')
const history = ref<GitCommit[]>([])
const replayCommit = ref('')
const replaySnapshot = ref<TemporalSnapshot>()
const replayLoading = ref(false)
const diffFrom = ref('')
const diffTo = ref('')
const diffFromKind = ref<'commit' | 'snapshot'>('commit')
const diffToKind = ref<'commit' | 'snapshot'>('commit')
const diffFromSnapshot = ref('')
const diffToSnapshot = ref('')
const timelineDiff = ref<TimelineDiff>()
const diffLoading = ref(false)
const diffError = ref('')
const selectedDiffTarget = ref('')
const isReplay = computed(() => Boolean(replaySnapshot.value && replayCommit.value))
const visibleEntities = computed(() => (isReplay.value ? replaySnapshot.value?.entities || [] : props.entities) as Entity[])

const characters = computed(() => visibleEntities.value.filter(e => e.type === 'character' && e.status === 'active'))
const timelines = computed(() => visibleEntities.value.filter(e => e.type === 'timeline' && e.status === 'active'))
const events = computed(() => visibleEntities.value.filter(e => e.type === 'event' && e.status === 'active'))
const selectedTimeSystem = computed(() => systems.value.find(item => item.id === selectedSystem.value))
const rangeSpan = computed(() => Math.max(1, rangeEnd.value - rangeStart.value))
const playheadPosition = computed(() => position(at.value))
const ticks = computed(() => Array.from({ length: 7 }, (_, index) => rangeStart.value + (rangeSpan.value * index) / 6))
const characterLanes = computed(() => characters.value.map(character => ({ character, lifespan: lifespans.value.find(item => item.character === character.id) })))

let graph: Core | undefined
function query() {
  const params = new URLSearchParams({ workspace: props.workspace })
  if (props.branch !== 'main') params.set('branch', props.branch)
  return params.toString()
}
function formatTime(value: number | null | undefined) { return formatWorldTime(value, selectedTimeSystem.value) }
function position(value: number | null | undefined) {
  if (value === null || value === undefined) return 0
  return Math.max(0, Math.min(100, ((Number(value) - rangeStart.value) / rangeSpan.value) * 100))
}
function barStyle(start: number | null | undefined, end: number | null | undefined) {
  const left = position(start)
  const right = end === null || end === undefined ? 100 : position(end)
  return { left: `${left}%`, width: `${Math.max(1.5, right - left)}%` }
}
function eventStyle(entry: TimelineEntry) { return barStyle(entry.start_value, entry.end_value ?? entry.start_value) }
function setAt(value: number) { at.value = Math.round(value); void loadSlice() }
function clampView() {
  if (rangeStart.value >= rangeEnd.value) rangeEnd.value = rangeStart.value + 1
  rangeStart.value = Math.max(dataMin.value, Math.min(rangeStart.value, dataMax.value - 1))
  rangeEnd.value = Math.min(dataMax.value, Math.max(rangeEnd.value, dataMin.value + 1))
  if (rangeStart.value >= rangeEnd.value) rangeEnd.value = rangeStart.value + 1
  at.value = Math.max(rangeStart.value, Math.min(at.value, rangeEnd.value))
}
function useFullRange() {
  rangeStart.value = dataMin.value
  rangeEnd.value = dataMax.value
  at.value = Math.max(rangeStart.value, Math.min(at.value, rangeEnd.value))
}
function updateDataRange() {
  const values = [...entries.value.flatMap(entry => [entry.start_value, entry.end_value]), ...lifespans.value.flatMap(span => [span.start_value, span.end_value]), at.value].filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  if (!values.length) { dataMin.value = 0; dataMax.value = 100 } else {
    const min = Math.min(...values), max = Math.max(...values), padding = Math.max(1, Math.ceil((max - min) * 0.08))
    dataMin.value = Math.floor(min - padding); dataMax.value = Math.ceil(max + padding)
    if (dataMax.value <= dataMin.value) dataMax.value = dataMin.value + 100
  }
  rangeStart.value = dataMin.value; rangeEnd.value = dataMax.value
  at.value = Math.max(rangeStart.value, Math.min(at.value, rangeEnd.value))
}
async function loadHistory() {
  try {
    const branch = props.branch !== 'main' ? `?branch=${encodeURIComponent(props.branch)}` : ''
    history.value = (await api<{ history: GitCommit[] }>(`workspaces/${props.workspace}/git/history/${branch}`)).history
  } catch (error) { emit('error', String(error)) }
}
function applyHistoricalSnapshot(snapshot: TemporalSnapshot) {
  systems.value = snapshot.time_systems || []
  if (!selectedSystem.value || !systems.value.some(system => system.id === selectedSystem.value)) selectedSystem.value = systems.value[0]?.id || ''
  const entities = new Map((snapshot.entities || []).map(entity => [String(entity.id), entity]))
  const axisEntries = snapshot.timeline_entries.filter(entry => String(entry.time_system) === String(selectedSystem.value) && !entry.archived && (!timelineId.value || String(entry.timeline) === String(timelineId.value)))
  entries.value = axisEntries.map(entry => ({
    id: String(entry.id), timeline: String(entry.timeline), timeline_title: entities.get(String(entry.timeline))?.title || '未命名时间线',
    event: String(entry.event), event_title: entities.get(String(entry.event))?.title || '未命名事件', start_value: Number(entry.start_value),
    end_value: entry.end_value === null || entry.end_value === undefined ? null : Number(entry.end_value), summary: entry.summary || '', sequence: Number(entry.sequence || 0),
    participant_details: (entry.participants || []).map(participant => ({ id: String(participant.entity), title: entities.get(String(participant.entity))?.title || '未命名实体', role: participant.role || '' })),
  }))
  lifespans.value = snapshot.character_lifespans.filter(span => String(span.time_system) === String(selectedSystem.value) && !span.archived).map(span => ({
    id: String(span.id), character: String(span.character), character_title: entities.get(String(span.character))?.title || '未命名人物', time_system: String(span.time_system),
    start_value: span.start_value === null || span.start_value === undefined ? null : Number(span.start_value), end_value: span.end_value === null || span.end_value === undefined ? null : Number(span.end_value), archived: false,
  }))
  updateDataRange()
}
async function selectReplay(commit: string) {
  replayCommit.value = commit
  if (!commit) {
    replaySnapshot.value = undefined
    await refresh()
    return
  }
  replayLoading.value = true
  try {
    const branch = props.branch !== 'main' ? `&branch=${encodeURIComponent(props.branch)}` : ''
    const data = await api<{ snapshot: TemporalSnapshot }>(`workspaces/${props.workspace}/git/temporal/?snapshot=1&commit=${encodeURIComponent(commit)}${branch}`)
    replaySnapshot.value = data.snapshot
    applyHistoricalSnapshot(data.snapshot)
    await loadSlice()
  } catch (error) { replayCommit.value = ''; replaySnapshot.value = undefined; emit('error', String(error)) }
  finally { replayLoading.value = false }
}
function diffRefValue(kind: 'commit' | 'snapshot', commit: string, snapshot: string) {
  return kind === 'commit' ? commit.trim() : snapshot.trim()
}
function loadTimelineDiff() {
  const from = diffRefValue(diffFromKind.value, diffFrom.value, diffFromSnapshot.value)
  const to = diffRefValue(diffToKind.value, diffTo.value, diffToSnapshot.value)
  if (!from && !to) { timelineDiff.value = undefined; diffError.value = ''; return }
  if (!from || !to) { diffError.value = '请选择或填写起点和终点版本'; return }
  if (diffFromKind.value === diffToKind.value && from === to) { diffError.value = '请选择两个不同的版本'; return }
  diffLoading.value = true; diffError.value = ''
  try {
    const params = new URLSearchParams()
    params.set(diffFromKind.value === 'commit' ? 'from_commit' : 'from_snapshot', from)
    params.set(diffToKind.value === 'commit' ? 'to_commit' : 'to_snapshot', to)
    params.set('include_lifecycles', 'true')
    params.set('include_participants', 'true')
    params.set('include_relations', 'true')
    params.set('include_time_system', 'true')
    if (props.branch !== 'main') params.set('branch', props.branch)
    const path = timelineId.value ? `workspaces/${props.workspace}/timelines/${timelineId.value}/diff/?${params}` : `workspaces/${props.workspace}/git/temporal/?${params}`
    void api<{ diff: TimelineDiff }>(path).then(response => { timelineDiff.value = response.diff }).catch(error => { diffError.value = String(error); timelineDiff.value = undefined }).finally(() => { diffLoading.value = false })
  } catch (error) { diffError.value = String(error); timelineDiff.value = undefined; diffLoading.value = false }
}
function record(value: unknown): Record<string, unknown> { return value && typeof value === 'object' ? value as Record<string, unknown> : {} }
function diffItemTitle(value: unknown) {
  const item = record(value); const after = record(item.after); const before = record(item.before)
  return String(item.title || after.title || before.title || item.event_title || '未命名事件')
}
function diffItemKind(value: unknown, fallback: string) { return String(record(value).change_kind || fallback) }
function diffItemTime(value: unknown) {
  const item = record(value); const after = record(item.after); const before = record(item.before)
  const valueAt = item.start_value ?? after.start_value ?? before.start_value
  return Number.isFinite(Number(valueAt)) ? formatTime(Number(valueAt)) : '未设定时间'
}
function changedEventLabel(value: unknown) {
  const item = record(value)
  const fields = Array.isArray(item.changed_fields) ? item.changed_fields : []
  return fields.map(field => String(record(field).field || '')).filter(Boolean).join('、') || '内容或关联发生变化'
}
function focusDiffTarget(id: string, kind: string, sourceId = '') {
  if (!id) return
  selectedDiffTarget.value = id
  emit('focus-target', { id, kind, ...(sourceId ? { sourceId } : {}) })
}
function selectDiffEvent(value: unknown) {
  const item = record(value); const after = record(item.after); const before = record(item.before)
  const numeric = Number(item.start_value ?? after.start_value ?? before.start_value)
  if (Number.isFinite(numeric)) { at.value = numeric; void loadSlice() }
  selectedEventId.value = String(item.id || '')
  focusDiffTarget(selectedEventId.value, 'event')
}
function selectDiffEntity(item: Record<string, unknown>, kind = 'entity') {
  const id = String(item.entity_id || item.character || item.source || item.target || item.id || '')
  if (id) focusDiffTarget(id, kind)
}
function selectDiffRelation(item: Record<string, unknown>) {
  const source = String(item.source || record(item.after).source || record(item.before).source || '')
  const target = String(item.target || record(item.after).target || record(item.before).target || '')
  focusDiffTarget(source || target, 'entity')
}
function selectEvidence(value: unknown) {
  const item = record(value); const evidence = Array.isArray(item.evidence) ? item.evidence : []
  const first = record(evidence[0])
  const sourceId = String(first.source_id || '')
  if (sourceId) focusDiffTarget(sourceId, String(first.source_type || 'evidence'), sourceId)
}
function diffChanges(value: unknown): Record<string, unknown>[] {
  const item = record(value)
  return [...(Array.isArray(item.added) ? item.added : []), ...(Array.isArray(item.removed) ? item.removed : []), ...(Array.isArray(item.changed) ? item.changed : [])].map(record)
}
function diffChangeSummary(value: unknown) {
  const item = record(value)
  return `新增 ${Array.isArray(item.added) ? item.added.length : 0} · 修改 ${Array.isArray(item.changed) ? item.changed.length : 0} · 移除 ${Array.isArray(item.removed) ? item.removed.length : 0}`
}
function evidenceText(value: unknown) {
  const item = record(value)
  const evidence = Array.isArray(item.evidence) ? item.evidence : []
  return evidence.length ? evidence.map(entry => { const ref = record(entry); return `${String(ref.source_type || 'source')}:${String(ref.source_id || '')}` }).join('、') : '当前比较结果'
}
async function refresh() {
  if (isReplay.value) {
    if (replaySnapshot.value) { applyHistoricalSnapshot(replaySnapshot.value); await loadSlice() }
    return
  }
  try {
    systems.value = await api(`time-systems/?workspace=${props.workspace}`)
    if (!selectedSystem.value || !systems.value.some(s => s.id === selectedSystem.value)) selectedSystem.value = systems.value[0]?.id || ''
    if (!selectedSystem.value) { entries.value = []; lifespans.value = []; slice.value = undefined; graph?.destroy(); graph = undefined; return }
    const args = `${query()}&time_system=${encodeURIComponent(selectedSystem.value)}`
    entries.value = await api<TimelineEntry[]>(`timeline-entries/?${args}${timelineId.value ? `&timeline=${encodeURIComponent(timelineId.value)}` : ''}`)
    lifespans.value = await api<Lifespan[]>(`character-lifespans/?${args}`)
    updateDataRange(); await loadSlice()
  } catch (error) { emit('error', String(error)) }
}
async function loadSlice() {
  if (!selectedSystem.value) return
  try {
    const params = new URLSearchParams({ workspace: props.workspace, time_system: selectedSystem.value, at: String(at.value) })
    if (timelineId.value) params.set('timeline', timelineId.value)
    if (props.branch !== 'main') params.set('branch', props.branch)
    if (isReplay.value) params.set('commit', replayCommit.value)
    slice.value = await api(`graph/slice/?${params}`)
    if (!host.value) return
    graph?.destroy()
    graph = cytoscape({ container: host.value, elements: [...slice.value.nodes, ...slice.value.edges], layout: { name: 'cose', animate: false, padding: 48 }, style: [
      { selector: 'node', style: { label: 'data(title)', 'background-color': '#718f7e', color: '#31483c', 'font-size': 12, 'text-valign': 'bottom', 'text-margin-y': 8, width: 38, height: 38 } },
      { selector: 'node[type = "event"]', style: { shape: 'round-rectangle', 'background-color': '#d0a15c' } },
      { selector: 'edge', style: { label: 'data(label)', width: 1.5, 'line-color': '#aebcaf', 'target-arrow-color': '#aebcaf', 'target-arrow-shape': 'triangle', 'curve-style': 'bezier', 'font-size': 10 } },
    ] })
  } catch (error) { emit('error', String(error)) }
}
async function createSystem() {
  const choice = await requestDialog({ title: '创建时间体系', confirmLabel: '创建体系', fields: [{ key: 'name', label: '时间体系名称', placeholder: '例如：王历' }, { key: 'unit', label: '最小计量单位', value: '日' }] }); if (!choice) return
  const { name, unit } = choice
  try { const created = await api<TimeSystem>('time-systems/', 'POST', { workspace: props.workspace, name: name.trim(), unit_name: unit, units: [], display_format: `{value} ${unit}` }); selectedSystem.value = created.id; await refresh() } catch (error) { emit('error', String(error)) }
}
async function saveLifespan() {
  if (!selectedSystem.value || !charId.value) return
  try {
    const params = new URLSearchParams({ workspace: props.workspace, time_system: selectedSystem.value }); if (props.branch !== 'main') params.set('branch', props.branch)
    const existing: Lifespan[] = await api(`character-lifespans/?${params}`), current = existing.find(item => item.character === charId.value)
    const payload = { workspace: props.workspace, ...(props.branch !== 'main' ? { branch: props.branch } : {}), character: charId.value, time_system: selectedSystem.value, start_value: Number(birth.value), end_value: death.value === undefined || death.value === null ? null : Number(death.value) }
    if (current) await api(`character-lifespans/${current.id}/?${query()}`, 'PATCH', payload); else await api('character-lifespans/', 'POST', payload)
    await refresh()
  } catch (error) { emit('error', String(error)) }
}
function resetEventForm() { selectedEventId.value = ''; timelineEntity.value = ''; eventEntity.value = ''; eventAt.value = 0; eventEnd.value = undefined; participants.value = []; participantRoles.value = {} }
function editEvent(entry: TimelineEntry) { selectedEventId.value = entry.id; timelineEntity.value = entry.timeline; eventEntity.value = entry.event; eventAt.value = Number(entry.start_value); eventEnd.value = entry.end_value === null ? undefined : Number(entry.end_value); participants.value = (entry.participant_details || []).map(item => item.id); participantRoles.value = Object.fromEntries((entry.participant_details || []).map(item => [item.id, item.role || ''])) }
function cancelEventEdit() { resetEventForm() }
async function deleteEvent(entry: TimelineEntry) {
  if (!await confirmAction(`从当前${props.branch === 'main' ? '主线' : '分支'}时间线移除“${entry.event_title}”？`, '移除时间线事件', '移除事件')) return
  try { await api(`timeline-entries/${entry.id}/?${query()}`, 'DELETE'); if (selectedEventId.value === entry.id) resetEventForm(); await refresh() } catch (error) { emit('error', String(error)) }
}
async function saveEvent() {
  if (!selectedSystem.value || !timelineEntity.value || !eventEntity.value) return
  try {
    const payload = { workspace: props.workspace, timeline: timelineEntity.value, event: eventEntity.value, time_system: selectedSystem.value, start_value: Number(eventAt.value), end_value: eventEnd.value === undefined || eventEnd.value === null ? Number(eventAt.value) : Number(eventEnd.value), participants: participants.value.map(entity => ({ entity, role: participantRoles.value[entity] || '' })) }
    const path = selectedEventId.value ? `timeline-entries/${selectedEventId.value}/?${query()}` : `timeline-entries/?${query()}`
    await api(path, selectedEventId.value ? 'PATCH' : 'POST', payload); at.value = Number(eventAt.value); resetEventForm(); await refresh()
  } catch (error) { emit('error', String(error)) }
}
watch(() => [props.workspace, props.branch], () => { replayCommit.value = ''; replaySnapshot.value = undefined; timelineDiff.value = undefined; diffFrom.value = ''; diffTo.value = ''; diffFromSnapshot.value = ''; diffToSnapshot.value = ''; void Promise.all([refresh(), loadHistory()]) })
watch(() => selectedSystem.value, () => { void refresh() })
onMounted(() => { void Promise.all([refresh(), loadHistory()]) })
onBeforeUnmount(() => graph?.destroy())
</script>

<template>
  <section class="timeline-panel">
    <TimeSystemEditor v-if="!isReplay" :workspace="workspace" :systems="systems" :selected-system="selectedSystem" @select="selectedSystem = $event; void refresh()" @updated="refresh" @error="emit('error', $event)" />
    <div v-else class="temporal-replay-readonly"><strong>历史版本只读</strong><span>时间体系、事件和人物状态来自 Git 提交，不会修改当前工作区。</span><button class="text-button" @click="selectReplay('')">返回当前状态</button></div>
    <header class="timeline-heading"><div><span class="eyebrow">TIME IS A WORLD'S OWN LANGUAGE</span><h2>时间线与人物生命周期</h2><p>把浓缩后的剧情事件、人物生命周期和某个时间切片放在同一条世界时间轴上。</p></div><div class="timeline-controls"><button :disabled="isReplay" @click="createSystem">＋ 时间体系</button><label class="timeline-replay">版本回放<select v-model="replayCommit" :disabled="replayLoading" @change="selectReplay(replayCommit)"><option value="">当前工作状态</option><option v-for="commit in history" :key="commit.hash" :value="commit.hash">{{ commit.hash.slice(0, 8) }} · {{ commit.message }}</option></select></label><select v-model="selectedSystem"><option value="">选择时间体系</option><option v-for="system in systems" :key="system.id" :value="system.id">{{ system.name }} · {{ system.epoch_label || system.unit_name }}</option></select><select v-model="timelineId" @change="refresh"><option value="">全部时间线</option><option v-for="item in timelines" :key="item.id" :value="item.id">{{ item.title }}</option></select><label>切片值 <input v-model.number="at" type="number" :min="rangeStart" :max="rangeEnd" @change="loadSlice"></label><button :disabled="!selectedSystem" @click="loadSlice">查看切片</button></div></header>
    <div v-if="!systems.length" class="timeline-empty"><p>先建立一个时间体系，世界才会拥有自己的纪年坐标。</p><button class="primary" @click="createSystem">创建时间体系</button></div>
    <template v-else>
      <section class="timeline-ruler-card" aria-label="时间轴浏览器"><div class="timeline-ruler-header"><div><strong>{{ selectedTimeSystem?.name }}</strong><span class="muted"> · {{ formatTime(rangeStart) }} — {{ formatTime(rangeEnd) }}</span></div><button class="text-button" @click="useFullRange">重置范围</button></div><div class="timeline-ruler"><span v-for="tick in ticks" :key="tick" class="timeline-tick" :style="{ left: `${position(tick)}%` }"><i></i><b>{{ formatTime(tick) }}</b></span><span class="timeline-playhead" :style="{ left: `${playheadPosition}%` }" aria-label="当前时间切片"></span></div><div class="timeline-range-inputs"><label>范围起点 <input v-model.number="rangeStart" type="number" :min="dataMin" :max="rangeEnd - 1" @change="clampView"></label><input v-model.number="rangeStart" type="range" :min="dataMin" :max="dataMax - 1" step="1" @input="clampView" aria-label="时间轴范围起点"><label>范围终点 <input v-model.number="rangeEnd" type="number" :min="rangeStart + 1" :max="dataMax" @change="clampView"></label><input v-model.number="rangeEnd" type="range" :min="dataMin + 1" :max="dataMax" step="1" @input="clampView" aria-label="时间轴范围终点"></div><div class="timeline-playhead-control"><label>当前切片 <input v-model.number="at" type="range" :min="rangeStart" :max="rangeEnd" step="1" @change="loadSlice" aria-label="当前时间切片滑块"></label><output>{{ formatTime(at) }}</output></div></section>
      <section class="timeline-storyboard" aria-label="事件与人物生命周期时间轴"><div class="storyboard-header"><strong>故事脉络</strong><span class="muted">点击事件定位时间切片；绿色条带表示生命周期，虚线表示尚未设定边界。</span></div><div class="storyboard-body"><div class="storyboard-grid" :style="{ left: `${playheadPosition}%` }"></div><div class="story-row event-story-row"><span class="story-label">事件</span><div class="story-track"><button v-for="entry in entries" :key="entry.id" class="story-event" :class="{ selected: selectedEventId === entry.id, current: at >= entry.start_value && (entry.end_value === null || at <= entry.end_value) }" :style="eventStyle(entry)" :title="`${entry.event_title} · ${formatTime(entry.start_value)}`" @click="setAt(entry.start_value); editEvent(entry)">{{ entry.event_title }}</button><span v-if="!entries.length" class="story-empty">还没有被放入时间线的事件</span></div></div><div v-for="lane in characterLanes" :key="lane.character.id" class="story-row"><span class="story-label">{{ lane.character.title }}</span><div class="story-track"><span v-if="lane.lifespan" class="life-bar" :class="{ alive: lane.lifespan.start_value !== null || lane.lifespan.end_value !== null }" :style="barStyle(lane.lifespan.start_value ?? rangeStart, lane.lifespan.end_value ?? rangeEnd)" :title="`${formatTime(lane.lifespan.start_value)} — ${lane.lifespan.end_value === null ? '至今' : formatTime(lane.lifespan.end_value)}`"></span><span v-else class="life-unknown">未设定生命周期</span></div></div><span class="story-playhead" :style="{ left: `${playheadPosition}%` }"></span></div></section>
      <section class="timeline-diff-panel" aria-label="时间线版本差异">
        <div class="timeline-diff-header"><div><strong>版本 / Snapshot Diff</strong><span class="muted">只读比较，不会修改历史快照。</span></div><button class="text-button" :disabled="diffLoading" @click="loadTimelineDiff">{{ diffLoading ? '比较中…' : '比较版本' }}</button></div>
        <div class="timeline-diff-selectors">
          <label>起点类型 <select v-model="diffFromKind"><option value="commit">Git commit</option><option value="snapshot">Snapshot</option></select></label>
          <label v-if="diffFromKind === 'commit'">起点 commit <select v-model="diffFrom"><option value="">选择起点</option><option v-for="commit in history" :key="`from-${commit.hash}`" :value="commit.hash">{{ commit.hash.slice(0, 8) }} · {{ commit.message }}</option></select></label>
          <label v-else>起点 snapshot <input v-model="diffFromSnapshot" placeholder="snapshot id / revision"></label>
          <label>终点类型 <select v-model="diffToKind"><option value="commit">Git commit</option><option value="snapshot">Snapshot</option></select></label>
          <label v-if="diffToKind === 'commit'">终点 commit <select v-model="diffTo"><option value="">选择终点</option><option v-for="commit in history" :key="`to-${commit.hash}`" :value="commit.hash">{{ commit.hash.slice(0, 8) }} · {{ commit.message }}</option></select></label>
          <label v-else>终点 snapshot <input v-model="diffToSnapshot" placeholder="snapshot id / revision"></label>
        </div>
        <p v-if="diffError" class="warning-text">{{ diffError }}</p>
        <div v-if="timelineDiff" class="timeline-diff-results">
          <div class="timeline-diff-meta"><span>{{ typeof timelineDiff.from === 'string' ? timelineDiff.from : timelineDiff.from_ref?.label || timelineDiff.from_ref?.id }}</span><span>→</span><span>{{ typeof timelineDiff.to === 'string' ? timelineDiff.to : timelineDiff.to_ref?.label || timelineDiff.to_ref?.id }}</span><span v-if="timelineDiff.schema_compatible === false" class="diff-warning">schema 不兼容</span></div>
          <div v-if="timelineDiff.warnings?.length" class="timeline-diff-warnings"><div v-for="(warning, index) in timelineDiff.warnings" :key="`warning-${index}`" class="diff-warning-row">⚠ {{ typeof warning === 'string' ? warning : warning.message }}</div></div>
          <div class="timeline-diff-grid">
            <article v-if="timelineDiff.addedEvents?.length" class="diff-group added"><h4>新增事件 · {{ timelineDiff.addedEvents.length }}</h4><button v-for="item in timelineDiff.addedEvents" :key="`add-${item.id}`" class="diff-item" @click="selectDiffEvent(item)"><b>{{ diffItemTitle(item) }}</b><span>{{ diffItemTime(item) }} · {{ diffItemKind(item, 'added') }}</span><small>证据：{{ evidenceText(item) }}</small></button></article>
            <article v-if="timelineDiff.removedEvents?.length" class="diff-group removed"><h4>删除事件 · {{ timelineDiff.removedEvents.length }}</h4><button v-for="item in timelineDiff.removedEvents" :key="`remove-${item.id}`" class="diff-item" @click="selectDiffEvent(item)"><b>{{ diffItemTitle(item) }}</b><span>{{ diffItemTime(item) }} · removed</span><small>证据：{{ evidenceText(item) }}</small></button></article>
            <article v-if="timelineDiff.changedEvents?.length" class="diff-group changed"><h4>修改 / 移动事件 · {{ timelineDiff.changedEvents.length }}</h4><button v-for="item in timelineDiff.changedEvents" :key="`change-${item.id}`" class="diff-item" @click="selectDiffEvent(item)"><b>{{ diffItemTitle(item) }}</b><span>{{ diffItemKind(item, 'modified') }} · {{ changedEventLabel(item) }}</span><small>{{ diffItemTime(item) }} · 证据：{{ evidenceText(item) }}</small></button></article>
            <article v-if="diffChanges(timelineDiff.lifecycleChanges).length" class="diff-group lifecycle"><h4>生命周期变化</h4><button v-for="item in diffChanges(timelineDiff.lifecycleChanges)" :key="`life-${item.id || item.character}`" class="diff-item" :class="{ selected: selectedDiffTarget === String(item.character || item.id || '') }" @click="selectDiffEntity(item)"><b>{{ item.character_title || item.character || '人物' }}</b><span>{{ item.change_kind || 'changed' }} · {{ changedEventLabel(item) }}</span><small>证据：{{ evidenceText(item) }}</small></button></article>
            <article v-if="timelineDiff.participantChanges?.length" class="diff-group participants"><h4>参与者变化 · {{ timelineDiff.participantChanges.length }}</h4><button v-for="item in timelineDiff.participantChanges" :key="`participant-${item.entry_id}`" class="diff-item" :class="{ selected: selectedDiffTarget === String(item.entry_id) }" @click="selectDiffEvent({ id: item.entry_id })"><b>{{ item.entry_title || item.entry_id }}</b><span>参与者 {{ item.before?.length || 0 }} → {{ item.after?.length || 0 }}</span><small>新增 {{ item.added?.length || 0 }} · 移除 {{ item.removed?.length || 0 }}</small></button></article>
            <article v-if="diffChanges(timelineDiff.relationChanges).length" class="diff-group relations"><h4>关系变化</h4><button v-for="item in diffChanges(timelineDiff.relationChanges)" :key="`relation-${item.id || item.source}-${item.target}`" class="diff-item" :class="{ selected: selectedDiffTarget === String(item.source || item.target || '') }" @click="selectDiffRelation(item)"><b>{{ item.relation_type || '关系' }}</b><span>{{ item.source || record(item.after).source || '' }} → {{ item.target || record(item.after).target || '' }} · {{ item.change_kind || 'changed' }}</span><small>证据：{{ evidenceText(item) }}</small></button></article>
          </div>
          <div v-if="timelineDiff.timeSystemChanges && Object.keys(timelineDiff.timeSystemChanges).length" class="diff-time-system"><strong>时间体系变化</strong><div v-for="([name, change]) in Object.entries(timelineDiff.timeSystemChanges)" :key="name" class="diff-line"><b>{{ name === 'time_systems' ? '时间体系' : '换算规则' }}</b><span>{{ diffChangeSummary(change) }}</span><button class="text-button" type="button" @click="selectEvidence(change)">查看证据</button></div></div>
          <p v-if="!timelineDiff.addedEvents?.length && !timelineDiff.removedEvents?.length && !timelineDiff.changedEvents?.length && !timelineDiff.warnings?.length" class="muted">两个版本没有可显示的变化。</p>
        </div>
      </section>
      <div class="timeline-workspace"><div ref="host" class="timeline-graph"></div><aside class="slice-summary"><h3>{{ selectedTimeSystem?.name }} · {{ formatTime(at) }}</h3><p class="muted">规范化坐标：{{ at }}</p><p>在世人物：{{ slice?.characters?.length ?? '…' }}</p><div v-for="person in slice?.characters || []" :key="person.id" class="life-row"><strong>{{ person.title }}</strong><span>{{ person.life_state === 'unspecified' ? '生命周期未设定' : '此时在世' }}</span></div><hr><h3>当前时间线事件</h3><div v-for="entry in entries" :key="entry.id" class="event-row"><div class="event-heading"><strong>{{ entry.event_title }}</strong><span class="event-actions"><button v-if="!isReplay" type="button" @click="editEvent(entry)">编辑</button><button v-if="!isReplay" type="button" @click="deleteEvent(entry)">移除</button></span></div><small>{{ formatTime(entry.start_value) }}{{ entry.end_value !== null ? ` – ${formatTime(entry.end_value)}` : '' }} · {{ entry.timeline_title }}</small><p>{{ entry.summary || '未填写摘要' }}</p><small v-if="entry.participant_details?.length">参与：{{ entry.participant_details.map(item => item.title + (item.role ? `（${item.role}）` : '')).join('、') }}</small></div><p v-if="!entries.length" class="muted">尚无事件条目</p></aside></div>
      <div v-if="!isReplay" class="temporal-editors"><form @submit.prevent="saveLifespan"><h3>设定人物生命周期</h3><select v-model="charId" required><option value="">选择人物</option><option v-for="person in characters" :key="person.id" :value="person.id">{{ person.title }}</option></select><label>开始值 <input v-model.number="birth" type="number" required></label><label>结束值（留空表示仍持续） <input v-model.number="death" type="number"></label><button :disabled="!charId">保存生命周期</button></form><form @submit.prevent="saveEvent"><h3>{{ selectedEventId ? '编辑时间线事件' : '将事件放入时间线' }}</h3><select v-model="timelineEntity" required><option value="">选择时间线</option><option v-for="item in timelines" :key="item.id" :value="item.id">{{ item.title }}</option></select><select v-model="eventEntity" required><option value="">选择事件</option><option v-for="item in events" :key="item.id" :value="item.id">{{ item.title }}</option></select><label>发生值 <input v-model.number="eventAt" type="number" required></label><label>结束值（可留空） <input v-model.number="eventEnd" type="number"></label><label>参与人物 <select v-model="participants" multiple><option v-for="person in characters" :key="person.id" :value="person.id">{{ person.title }}</option></select></label><div v-if="participants.length" class="participant-roles"><label v-for="person in characters.filter(item => participants.includes(item.id))" :key="person.id">{{ person.title }}的角色 <input v-model="participantRoles[person.id]" placeholder="例如：见证者"></label></div><div class="form-actions"><button :disabled="!timelineEntity || !eventEntity">{{ selectedEventId ? '保存事件修改' : '保存事件脉络' }}</button><button v-if="selectedEventId" type="button" @click="cancelEventEdit">取消编辑</button></div></form></div>
    </template>
  </section>
</template>
