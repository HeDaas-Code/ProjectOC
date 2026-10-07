<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount, watch } from 'vue'
import cytoscape, { type Core } from 'cytoscape'
import { api } from '../services/api'
import { entityTypes, type Entity } from '../types'
import GraphCanvasSurface from './GraphCanvasSurface.vue'
import type { Canvas } from '../types'

type GraphNode = { id: string; title: string; type: string; status?: string }
type GraphEdge = { id: string; source: string; target: string; label: string }
type PathResult = { nodes: GraphNode[]; edges: { id: string; type: string }[] }
type ImpactItem = { id: string; title: string; degree: number }

const props = defineProps<{ workspace: string; branch?: string; revision: string; selectedId?: string }>()
const emit = defineEmits<{ error: [message: string] }>()
const host = ref<HTMLElement>(), filter = ref(''), entity = ref<Entity>(), links = ref<any>()
const graphNodes = ref<GraphNode[]>([]), graphEdges = ref<GraphEdge[]>([]), pathTarget = ref(''), path = ref<PathResult>(), pathSource = ref(''), impact = ref<ImpactItem[]>([]), analysisLoading = ref(false), showArchived = ref(true), showIsolated = ref(true)
const showComposer = ref(false), relationSource = ref(''), relationTarget = ref(''), relationType = ref('LINKED_TO'), relationReason = ref(''), proposalMessage = ref(''), relationProposals = ref<any[]>([]), showProposals = ref(false), committingProposal = ref(false), selectedRelation = ref<GraphEdge>()
const graphCanvas = ref<Canvas>(), view = ref<'canvas' | 'analysis'>('canvas')
let graph: Core | undefined
const pathTargetOptions = computed(() => graphNodes.value.filter(node => node.id !== entity.value?.id))
const branchQuery = computed(() => props.branch && props.branch !== 'main' ? `&branch=${encodeURIComponent(props.branch)}` : '')
const visibleNodes = computed(() => graphNodes.value.filter(node => (showArchived.value || node.status !== 'archived') && (showIsolated.value || graphEdges.value.some(edge => edge.source === node.id || edge.target === node.id))))

async function select(id: string) {
  try {
    const params = new URLSearchParams({ workspace: props.workspace })
    if (props.branch && props.branch !== 'main') params.set('branch', props.branch)
    const query = `?${params.toString()}`
    entity.value = await api(`entities/${id}/${query}`)
    links.value = await api(`entities/${id}/links/${query}`)
    path.value = undefined
    pathSource.value = ''
    impact.value = []
    pathTarget.value = ''
  } catch (e) { emit('error', String(e)) }
}

async function load() {
  try {
    const data = await api<{ nodes: { data: GraphNode }[]; edges: any[] }>(`graph/?workspace=${props.workspace}${filter.value ? `&type=${filter.value}` : ''}${branchQuery.value}`)
    graphNodes.value = data.nodes.map(node => node.data)
    graphEdges.value = data.edges.map(edge => ({ id: edge.data.id, source: edge.data.source, target: edge.data.target, label: edge.data.label }))
    if (!graphCanvas.value) graphCanvas.value = await api<Canvas>('canvases/graph/', 'POST', { workspace: props.workspace, branch: props.branch || 'main' })
    if (pathTarget.value && !graphNodes.value.some(node => node.id === pathTarget.value)) pathTarget.value = ''
    graph?.destroy()
    if (view.value !== 'analysis' || !host.value) return
    graph = cytoscape({ container: host.value, elements: [...data.nodes, ...data.edges], layout: { name: 'cose', animate: false, padding: 60 }, style: [
      { selector: 'node', style: { label: 'data(title)', 'background-color': '#658675', color: '#344a42', 'font-size': 12, 'text-valign': 'bottom', 'text-margin-y': 9, width: 35, height: 35 } },
      { selector: 'edge', style: { label: 'data(label)', width: 1.5, 'line-color': '#b7c5bb', 'target-arrow-color': '#b7c5bb', 'target-arrow-shape': 'triangle', 'curve-style': 'bezier', 'font-size': 10, color: '#7a827a' } },
      { selector: 'node[type = "floating_tip"]', style: { shape: 'diamond', 'background-color': '#d0a15c' } },
      ...Object.keys(entityTypes).map((type, i) => ({ selector: `node[type = "${type}"]`, style: { 'background-color': ['#658675','#9b83bb','#699eb0','#c68d76','#c0ad71','#7ea187','#9b8c82','#d0a15c'][i] } })),
      { selector: 'edge[relationType = "CONFLICTS_WITH"]', style: { 'line-color': '#bf6c5d', 'line-style': 'dashed' as const } },
    ] })
    graph.on('tap', 'node', e => void select(e.target.id()))
  } catch (e) { emit('error', String(e)) }
}

async function createRelationProposal() {
  if (!graphCanvas.value || !relationSource.value || !relationTarget.value || relationSource.value === relationTarget.value) return
  try {
    await api(`canvases/${graphCanvas.value.id}/graph-relation-proposals/`, 'POST', { source_entity: relationSource.value, target_entity: relationTarget.value, relation_type: relationType.value, reason: relationReason.value })
    proposalMessage.value = '关系提案已保存，等待审核'; relationReason.value = ''; showComposer.value = false; await loadProposals()
  } catch (e) { emit('error', String(e)) }
}
async function loadProposals() { if (!graphCanvas.value) return; relationProposals.value = (await api<any[]>(`canvases/${graphCanvas.value.id}/graph-relation-proposals/`)).filter(p => p.status === 'pending') }
async function reviewProposal(id: string, status: 'rejected') { if (!graphCanvas.value) return; await api(`relation-proposals/${id}/`, 'PATCH', { status }); await loadProposals() }
async function commitProposal(id: string) { if (!graphCanvas.value || committingProposal.value) return; committingProposal.value = true; try { const preview = await api<any>(`canvases/${graphCanvas.value.id}/graph-preview/`, 'POST', { relation_proposal_ids: [id] }); await api(`canvases/${graphCanvas.value.id}/graph-commit/`, 'POST', { relation_proposal_ids: [id], idempotency_key: `graph-${id}-${Date.now()}`, preview_token: preview.preview_token }); proposalMessage.value = '关系提案已接受并写入正式关系'; await load(); await loadProposals() } catch (e) { emit('error', String(e)) } finally { committingProposal.value = false } }

async function findPath() {
  if (!entity.value || !pathTarget.value) return
  analysisLoading.value = true
  try {
    const query = new URLSearchParams({ workspace: props.workspace, from: entity.value.id, to: pathTarget.value })
    if (props.branch && props.branch !== 'main') query.set('branch', props.branch)
    const result = await api<{ path: PathResult | null; source: string }>(`graph/path/?${query}`)
    path.value = result.path || undefined
    pathSource.value = result.source
  } catch (e) { emit('error', String(e)) }
  finally { analysisLoading.value = false }
}

async function analyzeImpact() {
  if (!entity.value) return
  analysisLoading.value = true
  try {
    const query = new URLSearchParams({ workspace: props.workspace, entity: entity.value.id })
    if (props.branch && props.branch !== 'main') query.set('branch', props.branch)
    const result = await api<{ items: ImpactItem[] }>(`graph/impact/?${query}`)
    impact.value = result.items
  } catch (e) { emit('error', String(e)) }
  finally { analysisLoading.value = false }
}

onMounted(() => { void load(); if (props.selectedId) void select(props.selectedId) })
watch(() => [props.workspace, props.branch, props.revision, filter.value], () => { graphCanvas.value = undefined; void load() })
watch(view, value => { if (value === 'analysis') void load() })
watch(() => props.selectedId, id => { if (id) void select(id) })
onBeforeUnmount(() => graph?.destroy())
</script>

<template>
  <section class="graph-panel">
    <header><h2>世界观图谱</h2><button :class="{ active: view === 'canvas' }" @click="view = 'canvas'">画板</button><button :class="{ active: view === 'analysis' }" @click="view = 'analysis'">分析</button><select v-model="filter"><option value="">全部实体类型</option><option v-for="(label, value) in entityTypes" :value="value">{{ label }}</option></select><label v-if="view === 'canvas'" class="graph-toggle"><input v-model="showArchived" type="checkbox">失效节点</label><label v-if="view === 'canvas'" class="graph-toggle"><input v-model="showIsolated" type="checkbox">孤立节点</label><button v-if="view === 'canvas'" @click="showComposer = true">创建关系</button><button v-if="view === 'canvas'" @click="showProposals = !showProposals; loadProposals()">图谱提案{{ relationProposals.length ? ` (${relationProposals.length})` : '' }}</button><button @click="load">刷新正式投影</button><span class="muted">{{ view === 'canvas' ? '正式设定快照 · 可协作编排' : '仅显示已确认关系 · 拖动节点探索' }}</span></header>
    <p v-if="proposalMessage" class="success-banner">{{ proposalMessage }}</p>
    <section v-if="showProposals" class="graph-proposal-list"><h3>待审核关系</h3><p v-if="!relationProposals.length" class="muted">暂无图谱关系提案</p><article v-for="proposal in relationProposals" :key="proposal.id"><strong>{{ proposal.source_title }} → {{ proposal.target_title }}</strong><span>{{ proposal.relation_type }} · {{ proposal.reason || '无说明' }}</span><footer><button @click="reviewProposal(proposal.id, 'rejected')">拒绝</button><button class="primary" :disabled="committingProposal" @click="commitProposal(proposal.id)">接受并写入</button></footer></article></section>
    <div v-if="showComposer" class="graph-proposal-dialog" role="dialog" aria-modal="true"><h3>创建关系提案</h3><label>源实体<select v-model="relationSource"><option value="">选择实体</option><option v-for="node in visibleNodes" :key="node.id" :value="node.id">{{ node.title }}</option></select></label><label>目标实体<select v-model="relationTarget"><option value="">选择实体</option><option v-for="node in visibleNodes" :key="node.id" :value="node.id">{{ node.title }}</option></select></label><label>关系类型<select v-model="relationType"><option v-for="(_, value) in { LINKED_TO: 1, DERIVES_FROM: 1, BELONGS_TO: 1, INFLUENCES: 1, CONFLICTS_WITH: 1 }" :value="value">{{ value }}</option></select></label><label>说明<textarea v-model="relationReason" /></label><footer><button @click="showComposer = false">取消</button><button class="primary" :disabled="!relationSource || !relationTarget" @click="createRelationProposal">提交审核</button></footer></div>
    <div class="graph-layout">
      <GraphCanvasSurface v-if="view === 'canvas' && graphCanvas" :key="graphCanvas.id" :canvas="graphCanvas" :nodes="visibleNodes" :edges="graphEdges" @select="select" @relation="id => selectedRelation = graphEdges.find(edge => edge.id === id)" @error="emit('error', $event)" /><div v-else ref="host" class="graph-host"/>
      <aside v-if="selectedRelation" class="entity-detail relation-detail"><span class="eyebrow">正式关系</span><h3>{{ selectedRelation.label }}</h3><p>{{ graphNodes.find(node => node.id === selectedRelation?.source)?.title }} → {{ graphNodes.find(node => node.id === selectedRelation?.target)?.title }}</p><small>关系边 {{ selectedRelation.id }} · 移动节点时自动跟随</small></aside>
      <aside v-if="entity" class="entity-detail">
        <span class="eyebrow">{{ entityTypes[entity.type] }}</span><h2>{{ entity.title }}</h2><p class="preserve">{{ entity.content }}</p>
        <h3>出链</h3><button v-for="link in links?.outgoing" @click="select(link.otherEntity.id)">{{ link.relationLabel }} → {{ link.otherEntity.title }}</button><p v-if="!links?.outgoing.length" class="muted">没有出链</p>
        <h3>反向链接</h3><button v-for="link in links?.incoming" @click="select(link.otherEntity.id)">{{ link.otherEntity.title }} → {{ link.relationLabel }}</button><p v-if="!links?.incoming.length" class="muted">没有反向链接 · 可能是孤立实体</p>
        <section class="graph-analysis"><h3>路径与影响分析</h3><label class="analysis-select">目标实体<select v-model="pathTarget"><option value="">选择目标</option><option v-for="node in pathTargetOptions" :key="node.id" :value="node.id">{{ node.title }}</option></select></label><button :disabled="!pathTarget || analysisLoading" @click="findPath">{{ analysisLoading ? '分析中…' : '寻找最短路径' }}</button><button :disabled="analysisLoading" @click="analyzeImpact">影响范围</button><div v-if="path" class="analysis-result"><small>路径来源：{{ pathSource }}</small><p>{{ path.nodes.map(node => node.title).join(' → ') }}</p><small>经过 {{ path.edges.length }} 条关系</small></div><p v-else-if="pathSource" class="muted">没有找到可达路径</p><ol v-if="impact.length" class="impact-list"><li v-for="item in impact" :key="item.id"><button @click="select(item.id)">{{ item.title }}</button><small>连接度 {{ item.degree }}</small></li></ol><p v-else-if="impact.length === 0 && pathSource === ''" class="muted">选择一种分析方式。</p></section>
        <hr/><small>{{ entity.git_path }}<br/>{{ entity.sync_status }} · {{ entity.commit_hash.slice(0,8) }}</small>
      </aside>
    </div>
  </section>
</template>
