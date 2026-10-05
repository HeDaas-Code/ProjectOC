<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ApiError, api } from '../services/api'
import { entityTypes, type EntityType } from '../types'
import {
  gitHealthLabel,
  maintenanceActionLabel,
  maintenanceModeLabel,
  maintenancePriorityLabel,
  pendingJobCount,
  projectionHealthLabel,
  type GitBranchHealth,
  type GitHealth,
  type MaintenanceReview,
  type MaintenanceProposalRequest,
  type MaintenanceProposalResponse,
  type ProjectionHealth,
  consistencyHealthLabel,
  type ConsistencyReport,
} from '../services/maintenance'

const props = defineProps<{ workspace: string; branch?: string; canvas?: string; canEdit?: boolean }>()
const emit = defineEmits<{
  error: [message: string]
  'focus-target': [id: string]
  'proposal-created': [proposal: MaintenanceProposalResponse]
}>()

const git = ref<GitHealth>()
const projection = ref<ProjectionHealth>()
const consistency = ref<ConsistencyReport>()
const review = ref<MaintenanceReview>()
const loading = ref(false)
const reviewLoading = ref(false)
const reviewUnavailable = ref(false)
const reviewError = ref('')
const rebuilding = ref(false)
const updatedAt = ref('')
const proposalDrafts = ref<Record<string, { title: string; content: string; entity_type: EntityType; idempotency_key: string; submitting: boolean; created?: boolean }>>({})
let refreshTimer: ReturnType<typeof setInterval> | undefined

const selectedBranch = computed<GitBranchHealth | undefined>(() => {
  if (!git.value || !props.branch || props.branch === 'main') return undefined
  return git.value.branches.find(item => item.id === props.branch || item.name === props.branch)
})
const pending = computed(() => pendingJobCount(git.value?.pending_jobs))
const hasWarnings = computed(() => Boolean(
  git.value && (!git.value.main_ref_exists || git.value.orphan_refs.length || pending.value) ||
  projection.value && (!projection.value.available || projection.value.last_job?.status === 'failed') ||
  consistency.value && (consistency.value.summary.error > 0 || consistency.value.summary.warning > 0),
))

function shortHash(value?: string) { return value ? value.slice(0, 10) : '—' }
function timeLabel() { return updatedAt.value ? new Date(updatedAt.value).toLocaleTimeString() : '尚未读取' }
function targetIds(issue: { entity_id?: string; relation_id?: string }) {
  return [issue.entity_id, issue.relation_id].filter((id): id is string => Boolean(id))
}
function focusTarget(id: string) { emit('focus-target', id) }
function suggestionKey(suggestion: { issue_code: string; title: string }) {
  return `${suggestion.issue_code}:${suggestion.title}`
}
function draftFor(suggestion: { issue_code: string; title: string }) {
  const key = suggestionKey(suggestion)
  if (!proposalDrafts.value[key]) {
    proposalDrafts.value[key] = {
      title: suggestion.title,
      content: '',
      entity_type: 'canonical_setting',
      idempotency_key: crypto.randomUUID(),
      submitting: false,
    }
  }
  return proposalDrafts.value[key]
}
async function createProposal(suggestion: { issue_code: string; title: string; reason: string; target_ids: string[] }) {
  if (!props.canvas || props.canEdit === false) return
  const draft = draftFor(suggestion)
  if (!draft.title.trim()) return
  draft.submitting = true
  reviewError.value = ''
  try {
    const payload: MaintenanceProposalRequest = {
      canvas: props.canvas,
      branch: props.branch || 'main',
      action: 'draft_proposal',
      issue_code: suggestion.issue_code,
      target_ids: suggestion.target_ids,
      title: draft.title.trim(),
      content: draft.content,
      entity_type: draft.entity_type,
      reason: suggestion.reason,
      idempotency_key: draft.idempotency_key,
    }
    const proposal = await api<MaintenanceProposalResponse>(`workspaces/${props.workspace}/maintenance-proposals/`, 'POST', payload)
    draft.created = true
    emit('proposal-created', proposal)
  } catch (error) {
    reviewError.value = String(error)
  } finally {
    draft.submitting = false
  }
}

async function load() {
  if (!props.workspace) return
  loading.value = true
  try {
    const branch = props.branch && props.branch !== 'main' ? `?branch=${encodeURIComponent(props.branch)}` : ''
    const [gitStatus, projectionStatus, consistencyStatus] = await Promise.all([
      api<GitHealth>(`workspaces/${props.workspace}/git/status/`),
      api<ProjectionHealth>(`workspaces/${props.workspace}/graph/projection/`),
      api<ConsistencyReport>(`workspaces/${props.workspace}/consistency/${branch}`),
    ])
    git.value = gitStatus
    projection.value = projectionStatus
    consistency.value = consistencyStatus
    updatedAt.value = new Date().toISOString()
  } catch (error) {
    emit('error', String(error))
  } finally {
    loading.value = false
  }
}

async function runMaintenanceReview() {
  if (!props.workspace || reviewLoading.value) return
  reviewLoading.value = true
  reviewUnavailable.value = false
  reviewError.value = ''
  try {
    const branch = props.branch && props.branch !== 'main' ? `?branch=${encodeURIComponent(props.branch)}` : ''
    review.value = await api<MaintenanceReview>(`workspaces/${props.workspace}/maintenance-review/${branch}`, 'POST', {})
  } catch (error) {
    if (error instanceof ApiError && error.status === 503) {
      reviewUnavailable.value = true
      reviewError.value = '维护 Agent 暂不可用；确定性一致性报告仍然保留。'
    } else {
      reviewError.value = String(error)
    }
  } finally {
    reviewLoading.value = false
  }
}

async function rebuildProjection() {
  rebuilding.value = true
  try {
    await api(`workspaces/${props.workspace}/graph/projection/`, 'POST', {})
    await load()
  } catch (error) {
    emit('error', String(error))
  } finally {
    rebuilding.value = false
  }
}

onMounted(() => {
  void load()
  refreshTimer = setInterval(() => void load(), 30_000)
})
onBeforeUnmount(() => { if (refreshTimer) clearInterval(refreshTimer) })
watch(() => [props.workspace, props.branch], () => {
  review.value = undefined
  reviewError.value = ''
  reviewUnavailable.value = false
  void load()
})
</script>

<template>
  <section class="maintenance-status" aria-labelledby="maintenance-status-title">
    <header class="maintenance-status-header">
      <div>
        <span class="eyebrow">SYSTEM HEALTH</span>
        <h3 id="maintenance-status-title">维护状态</h3>
        <p class="muted">数据库是事实源，Git 与图谱投影都可以安全重试。</p>
      </div>
      <div class="maintenance-actions">
        <span class="maintenance-updated">{{ loading ? '读取中…' : `更新于 ${timeLabel()}` }}</span>
        <button type="button" :disabled="loading" @click="load">刷新</button>
      </div>
    </header>

    <div v-if="!git || !projection || !consistency" class="maintenance-loading">正在读取 Git、图谱与一致性状态…</div>
    <template v-else>
      <div class="maintenance-cards">
        <article class="maintenance-card" :class="{ warning: !git.main_ref_exists || git.orphan_refs.length }">
          <div class="maintenance-card-title"><strong>Git / main</strong><span>{{ gitHealthLabel(git) }}</span></div>
          <code>{{ shortHash(git.main_commit) }}</code>
          <small>{{ git.repository }}</small>
        </article>
        <article class="maintenance-card" :class="{ warning: pending > 0 }">
          <div class="maintenance-card-title"><strong>同步任务</strong><span>{{ pending ? `${pending} 个待处理` : '无待处理' }}</span></div>
          <small v-if="pending">{{ Object.entries(git.pending_jobs).map(([key, value]) => `${key}: ${value}`).join(' · ') }}</small>
          <small v-else>数据库、Git 与投影没有等待中的任务。</small>
        </article>
        <article class="maintenance-card" :class="{ warning: !projection.available || projection.last_job?.status === 'failed' }">
          <div class="maintenance-card-title"><strong>图谱投影</strong><span>{{ projectionHealthLabel(projection) }}</span></div>
          <small v-if="projection.error">{{ projection.error }}</small>
          <small v-else-if="projection.last_job">最近任务：{{ projection.last_job.status }} · {{ projection.last_job.attempts }} 次尝试</small>
          <small v-else>尚无投影任务记录。</small>
          <button v-if="!projection.available || projection.last_job?.status === 'failed'" type="button" :disabled="rebuilding" @click="rebuildProjection">{{ rebuilding ? '重建中…' : '请求重建' }}</button>
        </article>
        <article class="maintenance-card" :class="{ warning: consistency.summary.error > 0 || consistency.summary.warning > 0 }">
          <div class="maintenance-card-title"><strong>世界观一致性</strong><span>{{ consistencyHealthLabel(consistency) }}</span></div>
          <code>{{ consistency.score }} / 100</code>
          <small>{{ consistency.entity_count }} 个实体 · {{ consistency.relation_count }} 条关系</small>
          <ul v-if="consistency.issues.length" class="consistency-issues">
            <li v-for="issue in consistency.issues.slice(0, 3)" :key="`${issue.code}-${issue.entity_id || issue.relation_id || issue.message}`" :class="issue.severity">
              <span>{{ issue.message }}</span>
              <button v-for="id in targetIds(issue)" :key="id" type="button" class="maintenance-target" @click="focusTarget(id)">定位 {{ id.slice(0, 8) }}</button>
            </li>
          </ul>
          <small v-if="consistency.issues.length > 3">还有 {{ consistency.issues.length - 3 }} 项待处理。</small>
          <small v-else-if="!consistency.issues.length">当前视图没有需要处理的问题。</small>
        </article>
      </div>

      <div v-if="selectedBranch" class="maintenance-branch">
        <div><strong>当前分支：{{ selectedBranch.name }}</strong><span>{{ selectedBranch.status }}</span></div>
        <small>ref：{{ selectedBranch.git_ref }} · head：{{ shortHash(selectedBranch.head_commit) }} · base：{{ shortHash(selectedBranch.base_commit) }}</small>
        <p v-if="!selectedBranch.ref_exists" class="warning-text">分支 ref 缺失，reconciliation 可以依据保存的 base commit 重建。</p>
        <p v-else-if="!selectedBranch.base_exists" class="warning-text">分支基线 commit 不存在，不能安全自动合并。</p>
        <p v-else-if="!selectedBranch.base_matches_head" class="muted">分支已经产生独立提交，合并前请查看差异。</p>
      </div>
      <div v-if="git.orphan_refs.length" class="maintenance-orphans">
        <strong>孤立 Git refs</strong>
        <code v-for="ref in git.orphan_refs" :key="ref">{{ ref }}</code>
      </div>

      <section class="maintenance-review" aria-labelledby="maintenance-review-title">
        <div class="maintenance-review-header">
          <div>
            <span class="eyebrow">AGENTIC MAINTENANCE</span>
            <h4 id="maintenance-review-title">让 Agent 帮你检查世界观</h4>
            <p class="muted">只读取当前分支的一致性报告和实体关系，建议不会自动修改、提交或接受任何正式设定。</p>
          </div>
          <button type="button" class="maintenance-review-button" :disabled="reviewLoading" @click="runMaintenanceReview">{{ reviewLoading ? '审阅中…' : review ? '重新审阅' : '开始审阅' }}</button>
        </div>
        <p v-if="reviewError" class="maintenance-review-error" role="alert">{{ reviewError }}</p>
        <p v-if="reviewUnavailable" class="maintenance-review-note">上游模型暂不可用。你仍可以依据上方的确定性报告继续工作；已有内容和提案没有被回滚。</p>
        <template v-if="review">
          <div class="maintenance-review-meta"><span>{{ maintenanceModeLabel(review.mode) }}</span><span>分支：{{ review.branch }}</span><span>建议仅供审核</span></div>
          <article class="maintenance-question">
            <div class="maintenance-review-label"><strong>下一步问题</strong><span>{{ maintenancePriorityLabel(review.question.priority) }}</span></div>
            <p>{{ review.question.text }}</p>
            <small>为什么：{{ review.question.reason }} · 可以跳过，不会阻止继续创作。</small>
          </article>
          <div v-if="review.suggestions.length" class="maintenance-suggestions">
            <article v-for="suggestion in review.suggestions" :key="`${suggestion.issue_code}-${suggestion.title}`" class="maintenance-suggestion">
              <div class="maintenance-review-label"><strong>{{ suggestion.title }}</strong><span>{{ maintenanceActionLabel(suggestion.action) }}</span></div>
              <p>{{ suggestion.reason }}</p>
              <small>依据问题：<code>{{ suggestion.issue_code }}</code></small>
              <div v-if="suggestion.target_ids.length" class="maintenance-targets">
                <button v-for="id in suggestion.target_ids" :key="id" type="button" class="maintenance-target" @click="focusTarget(id)">定位 {{ id.slice(0, 8) }}</button>
              </div>
              <div v-if="suggestion.action === 'draft_proposal' && props.canvas && props.canEdit !== false" class="maintenance-proposal-draft">
                <template v-if="!draftFor(suggestion).created">
                  <label>提案标题<input v-model="draftFor(suggestion).title" maxlength="500" /></label>
                  <label>候选内容<textarea v-model="draftFor(suggestion).content" rows="3" placeholder="由你确认要进入暂存区的候选内容" /></label>
                  <label>实体类型<select v-model="draftFor(suggestion).entity_type"><option v-for="(label, value) in entityTypes" :key="value" :value="value">{{ label }}</option></select></label>
                  <button type="button" class="maintenance-proposal-button" :disabled="draftFor(suggestion).submitting || !draftFor(suggestion).title.trim()" @click="createProposal(suggestion)">{{ draftFor(suggestion).submitting ? '创建中…' : '创建待审核提案' }}</button>
                </template>
                <small v-else class="maintenance-proposal-created">✓ 已创建待审核提案；仍需在提案审核面板中确认。</small>
              </div>
            </article>
          </div>
          <p v-else class="muted">Agent 没有生成额外建议。</p>
        </template>
      </section>
      <p v-if="hasWarnings" class="maintenance-warning">存在需要关注的维护状态，但不影响已经确认的 PostgreSQL 内容。</p>
    </template>
  </section>
</template>
