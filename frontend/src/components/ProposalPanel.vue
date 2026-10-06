<script setup lang="ts">
import StyledDialog from './StyledDialog.vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useWorkbench } from '../stores/workbench'
import { api } from '../services/api'
import { entityTypes, type Proposal } from '../types'

const store = useWorkbench()
const chosen = ref<string[]>([])
const chosenRelations = ref<string[]>([])
const preview = ref<any>()
const timeSystems = ref<{ id: string; name: string }[]>([])
const busy = ref(false)
const key = ref('')
const source = ref('')
const target = ref('')
const relationType = ref('LINKED_TO')
const pending = computed(() => store.proposals.filter(proposal => proposal.status === 'pending'))
const targetProposals = computed(() => pending.value.filter(proposal => proposal.id !== source.value))
const relationTypeOptions = [
  'LINKED_TO', 'DERIVES_FROM', 'BELONGS_TO', 'INFLUENCES', 'CONFLICTS_WITH',
  'EVOLVES_TO', 'KNOWS', 'LOCATED_AT', 'OWNS', 'PARTICIPATES_IN',
  'PARENT_OF', 'INHERITS_FROM',
]

watch(() => store.canvas?.id, () => {
  chosen.value = []
  chosenRelations.value = []
  preview.value = undefined
})
watch([chosen, chosenRelations, () => store.proposals.map(p => `${p.id}:${p.updated_at}`).join('|')], () => {
  // A preview token applies only to the exact current selection and proposal
  // contents. Any change forces a fresh review.
  preview.value = undefined
}, { deep: true })

async function loadTimeSystems() { if (store.workspace) timeSystems.value = await api(`time-systems/?workspace=${store.workspace.id}`) }

async function saveRelationTime(relation: any) { try { await api(`relation-proposals/${relation.id}/`, 'PATCH', { time_system: relation.time_system || null, valid_from: relation.valid_from, valid_to: relation.valid_to }); await store.refreshProposals() } catch (error) { store.error = String(error) } }

onMounted(() => { void loadTimeSystems() })
watch(() => store.workspace?.id, () => { void loadTimeSystems() })

async function save(proposal: Proposal, status = proposal.status) {
  try {
    await api(`proposals/${proposal.id}/`, 'PATCH', {
      title: proposal.title,
      content: proposal.content,
      entity_type: proposal.entity_type,
      status,
    })
    await store.refreshProposals()
  } catch (error) {
    store.error = String(error)
  }
}

async function create() {
  if (!store.workspace || !store.canvas) return
  try {
    await api('proposals/', 'POST', {
      workspace: store.workspace.id,
      canvas: store.canvas.id,
      entity_type: 'canonical_setting',
      title: '未命名设定',
      content: '',
    })
    await store.refreshProposals()
  } catch (error) {
    store.error = String(error)
  }
}

async function addRelation() {
  if (!source.value || !target.value) return
  try {
    await api('relation-proposals/', 'POST', {
      source_proposal: source.value,
      ...(target.value.startsWith('e:')
        ? { target_entity: target.value.slice(2) }
        : { target_proposal: target.value }),
      relation_type: relationType.value,
    })
    source.value = ''
    target.value = ''
    await store.refreshProposals()
  } catch (error) {
    store.error = String(error)
  }
}

async function showPreview() {
  if (!store.canvas) return
  busy.value = true
  try {
    // Persist edited fields before obtaining the exact confirmation token.
    for (const proposal of store.proposals.filter(item => chosen.value.includes(item.id))) {
      await api(`proposals/${proposal.id}/`, 'PATCH', {
        title: proposal.title,
        content: proposal.content,
        entity_type: proposal.entity_type,
      })
    }
    await store.refreshProposals()
    preview.value = await api(`canvases/${store.canvas.id}/preview/`, 'POST', {
      proposal_ids: chosen.value,
      relation_proposal_ids: chosenRelations.value,
    })
    key.value = crypto.randomUUID()
  } catch (error) {
    store.error = String(error)
  } finally {
    busy.value = false
  }
}

async function commit() {
  if (!store.canvas || !preview.value) return
  busy.value = true
  try {
    await api(`canvases/${store.canvas.id}/commit/`, 'POST', {
      proposal_ids: chosen.value,
      relation_proposal_ids: chosenRelations.value,
      idempotency_key: key.value,
      preview_token: preview.value.preview_token,
    })
    preview.value = undefined
    chosen.value = []
    chosenRelations.value = []
    await store.refreshProposals()
    await store.refreshWorld()
  } catch (error) {
    store.error = String(error)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="proposal-panel">
    <div class="panel-heading">
      <span class="eyebrow">REVIEW BEFORE REALITY</span>
      <h2>待定的世界</h2>
      <p>只提交你明确选择的实体与关系。</p>
      <button @click="create" :disabled="!store.canvas">＋ 手动实体草稿</button>
    </div>

    <div class="proposal-list">
      <article v-for="proposal in store.proposals" :key="proposal.id" class="proposal-card">
        <div class="proposal-meta">
          <label>
            <input
              v-if="proposal.status === 'pending'"
              v-model="chosen"
              type="checkbox"
              :value="proposal.id"
            />
            {{ proposal.status === 'accepted' ? '✓ 已入库' : proposal.status === 'rejected' ? '已拒绝' : '待审核' }}
          </label>
          <select v-model="proposal.entity_type" :disabled="proposal.status === 'accepted'" aria-label="实体类型">
            <option v-for="(label, value) in entityTypes" :key="value" :value="value">{{ label }}</option>
          </select>
        </div>
        <input v-model="proposal.title" class="title-input" aria-label="提案标题" :disabled="proposal.status === 'accepted'" />
        <textarea v-model="proposal.content" rows="3" aria-label="提案内容" :disabled="proposal.status === 'accepted'" />
        <p v-for="conflict in proposal.conflicts" :key="conflict.message" class="warning">⚠ {{ conflict.message }}</p>
        <div v-if="proposal.status !== 'accepted'" class="actions">
          <button @click="save(proposal, 'pending')">保存草稿</button>
          <button @click="save(proposal, 'rejected')">拒绝</button>
        </div>
      </article>
      <p v-if="!store.proposals.length" class="muted">还没有提案。开始对话，或手动添加一个设定。</p>

      <h3>建议关系 <small>默认不选中</small></h3>
      <article v-for="relation in store.relations" :key="relation.id" class="relation-card">
        <label>
          <input v-model="chosenRelations" type="checkbox" :value="relation.id" :disabled="relation.status !== 'pending'" />
          {{ store.proposals.find(proposal => proposal.id === relation.source_proposal)?.title || '已提交实体' }}
          → {{ relation.target_title }}
        </label>
        <small>{{ relation.relation_type }} · {{ relation.status }}</small>
        <p>{{ relation.reason }}</p>
        <div class="relation-time-fields">
          <select v-model="relation.time_system" :disabled="relation.status !== 'pending'" aria-label="关系所属时间体系"><option :value="null">无时间切片（持续关系）</option><option v-for="axis in timeSystems" :key="axis.id" :value="axis.id">{{ axis.name }}</option></select>
          <label>有效起点 <input v-model.number="relation.valid_from" type="number" :disabled="relation.status !== 'pending' || !relation.time_system" /></label>
          <label>有效终点 <input v-model.number="relation.valid_to" type="number" :disabled="relation.status !== 'pending' || !relation.time_system" /></label>
          <button v-if="relation.status === 'pending'" @click="saveRelationTime(relation)">保存时间关系</button>
        </div>
      </article>

      <details>
        <summary>＋ 手动连接</summary>
        <select v-model="source" aria-label="关系源">
          <option value="">源提案</option>
          <option v-for="proposal in pending" :key="proposal.id" :value="proposal.id">{{ proposal.title }}</option>
        </select>
        <select v-model="target" aria-label="关系目标">
          <option value="">目标实体 / 提案</option>
          <option v-for="proposal in targetProposals" :key="proposal.id" :value="proposal.id">草稿：{{ proposal.title }}</option>
          <option v-for="entity in store.entities" :key="entity.id" :value="`e:${entity.id}`">正式：{{ entity.title }}</option>
        </select>
        <select v-model="relationType" aria-label="关系类型">
          <option v-for="type in relationTypeOptions" :key="type" :value="type">{{ type }}</option>
        </select>
        <button @click="addRelation" :disabled="!source || !target">创建关系提案</button>
      </details>
    </div>

    <footer class="review-footer">
      <span>{{ chosen.length }} 个实体 · {{ chosenRelations.length }} 条关系</span>
      <button class="primary" @click="showPreview" :disabled="busy || (!chosen.length && !chosenRelations.length)">预览提交 →</button>
    </footer>

    <StyledDialog v-if="preview" :open="true" title="确认这次世界观变更" :busy="busy" wide @close="preview = undefined">
        <p>只将下面的变更写入正式库，并创建一次 Git 同步任务。</p>
        <pre>{{ preview.diff }}</pre>
        <div class="actions">
          <button :disabled="busy" @click="preview = undefined">返回修改</button>
          <button class="primary" :disabled="busy" @click="commit">{{ busy ? '正在提交…' : '确认提交' }}</button>
        </div>
    </StyledDialog>
  </div>
</template>
