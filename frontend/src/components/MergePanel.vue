<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, ApiError } from '../services/api'
import { useMergeSuggestions } from '../services/mergeSuggestions'

type FieldChange = { base: unknown; main: unknown; branch: unknown; state: string }
type Conflict = {
  branch_entity?: string
  branch_relation?: string
  title?: string
  kind: string
  main?: Record<string, unknown> | null
  branch?: Record<string, unknown> | null
  field_changes?: Record<string, FieldChange>
  conflicting_fields?: string[]
}
type Preview = {
  branch: string
  additions: unknown[]
  conflicts: Conflict[]
  entity_changes: Array<Record<string, any>>
  relation_changes: Array<Record<string, any>>
  preview_token: string
}

const props = defineProps<{ branchId: string; branchName: string }>()
const emit = defineEmits<{ merged: []; cancel: []; error: [message: string] }>()
const preview = ref<Preview>()
const decisions = ref<Record<string, any>>({})
const customValues = ref<Record<string, string>>({})
const loading = ref(true)
const submitting = ref(false)
const error = ref('')
const stalePreview = ref(false)
const selectedModel = ref('')
const models = ref<Array<{ id: string }>>([])
const modelNotice = ref('')
const { suggestions, loading: suggestionLoading, error: suggestionError,
  applied: suggestionApplied, stale: staleSuggestions, reset: resetSuggestions,
  request: requestSuggestions, apply: applySuggestions } = useMergeSuggestions(() => preview.value)
let loadGeneration = 0

const conflictCount = computed(() => preview.value?.conflicts.length || 0)
function keyOf(conflict: Conflict) { return conflict.branch_entity || conflict.branch_relation || '' }
function display(value: unknown) {
  if (value === null || value === undefined) return '（不存在）'
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}
function fieldsFor(conflict: Conflict) { return conflict.conflicting_fields || [] }
function isEntity(conflict: Conflict) { return Boolean(conflict.branch_entity) }
function defaultDecision(conflict: Conflict) {
  if (conflict.kind === 'duplicate_title') return 'keep_both'
  if (!fieldsFor(conflict).length) return 'keep_branch'
  return { fields: Object.fromEntries(fieldsFor(conflict).map(field => [field, 'keep_main'])) }
}
function choice(conflict: Conflict, field: string) {
  const value = decisions.value[keyOf(conflict)]
  if (typeof value === 'string') return value
  return value?.fields?.[field] || 'keep_main'
}
function setChoice(conflict: Conflict, field: string, value: string) {
  const key = keyOf(conflict)
  if (typeof decisions.value[key] === 'string') decisions.value[key] = { fields: {} }
  decisions.value[key] ||= { fields: {} }
  decisions.value[key].fields[field] = value
}
function customKey(conflict: Conflict, field: string) { return `${keyOf(conflict)}:${field}` }
function parseCustom(raw: string) {
  try { return JSON.parse(raw) } catch { return raw }
}
function resolutionFor(conflict: Conflict) {
  const key = keyOf(conflict)
  const value = decisions.value[key]
  if (conflict.kind === 'duplicate_title') return value || 'keep_both'
  if (typeof value === 'string') return value
  const fields = { ...(value?.fields || {}) }
  for (const field of fieldsFor(conflict)) {
    if (fields[field] === 'custom') fields[field] = { value: parseCustom(customValues.value[customKey(conflict, field)] || '') }
  }
  return { fields }
}

async function load() {
  const id = ++loadGeneration
  resetSuggestions()
  loading.value = true; error.value = ''
  try {
    const result = await api<Preview>(`branches/${props.branchId}/merge-preview/`, 'POST', {})
    if (id !== loadGeneration) return
    preview.value = result
    stalePreview.value = false
    customValues.value = {}
    decisions.value = Object.fromEntries(result.conflicts.map(conflict => [keyOf(conflict), defaultDecision(conflict)]))
  } catch (exception) { if (id === loadGeneration) { error.value = String(exception); emit('error', error.value) } }
  finally { if (id === loadGeneration) loading.value = false }
}
async function submit() {
  if (!preview.value || stalePreview.value || staleSuggestions.value) return
  submitting.value = true; error.value = ''
  try {
    const resolutions = Object.fromEntries(preview.value.conflicts.map(conflict => [keyOf(conflict), resolutionFor(conflict)]))
    await api(`branches/${props.branchId}/merge/`, 'POST', { preview_token: preview.value.preview_token, resolutions })
    emit('merged')
  } catch (exception) {
    error.value = String(exception); emit('error', error.value)
    stalePreview.value = exception instanceof ApiError && exception.status === 409
  }
  finally { submitting.value = false }
}
function cancel() { emit('cancel') }
watch(() => props.branchId, () => { preview.value = undefined; void load() }, { immediate: true })
void api<{ models: Array<{ id: string }>; error?: string }>(`ai/providers/default/models/${localStorage.getItem('oc:workspace') ? `?workspace=${encodeURIComponent(localStorage.getItem('oc:workspace')!)}` : ''}`)
  .then(result => { models.value = result.models; modelNotice.value = result.error ? '模型列表不可用，使用后端默认模型或已配置候选。' : '' })
  .catch(() => { modelNotice.value = '模型列表不可用，仍可使用后端默认模型。' })
</script>

<template>
  <section class="merge-panel">
    <header class="merge-header">
      <div><span class="eyebrow">THREE-WAY MERGE REVIEW</span><h2>合并工作分支：{{ branchName }}</h2><p>非冲突字段会自动合并；冲突字段必须由你确认，最终值也可以直接编辑。</p></div>
      <button class="text-button" @click="cancel">关闭</button>
    </header>
    <p v-if="loading" class="muted">正在读取分支差异…</p>
    <p v-if="error" class="error-banner">{{ error }} <button @click="load">重新读取</button></p>
    <template v-if="!loading && preview">
      <p v-if="stalePreview || staleSuggestions" class="error-banner">预览已过期；当前选择仍保留供参考。重新读取会重置选择。<button @click="load">重新读取差异</button></p>
      <div class="merge-summary"><span>新增实体 {{ preview.additions.length }}</span><span>实体变更 {{ preview.entity_changes.filter(c => c.branch_changed).length }}</span><span>关系变更 {{ preview.relation_changes.filter(c => c.branch_changed).length }}</span><span :class="{ warning: conflictCount }">冲突 {{ conflictCount }}</span></div>
      <div v-if="conflictCount" class="merge-ai-tools">
        <div>
          <strong>AI 合并建议</strong>
          <p class="muted">只生成审核建议，不会直接修改正式世界观；应用后仍需你确认合并。</p>
        </div>
        <div class="merge-ai-actions">
          <label>建议模型 <select v-model="selectedModel" :disabled="suggestionLoading"><option value="">后端默认模型</option><option v-for="model in models" :key="model.id" :value="model.id">{{ model.id }}</option></select></label>
          <button :disabled="suggestionLoading || submitting || staleSuggestions || stalePreview" @click="requestSuggestions(selectedModel)">{{ suggestionLoading ? '正在分析…' : '生成建议' }}</button>
          <button v-if="suggestions.length" class="secondary" :disabled="suggestionApplied || suggestionLoading || submitting || staleSuggestions || stalePreview" @click="applySuggestions(decisions)">{{ suggestionApplied ? '建议已填入' : `应用 ${suggestions.length} 条建议` }}</button>
        </div>
        <p v-if="modelNotice" class="muted">{{ modelNotice }}</p>
        <p v-if="suggestionError" class="warning-text">{{ suggestionError }}</p>
        <ul v-if="suggestions.length" class="merge-suggestions">
          <li v-for="suggestion in suggestions" :key="suggestion.conflict_id">
            <strong>{{ preview.conflicts.find(c => keyOf(c) === suggestion.conflict_id)?.title || suggestion.conflict_id }}</strong>
            · {{ suggestion.choice }} · 模型自评 {{ Math.round(suggestion.confidence * 100) }}%（非可靠性保证）
            <p>{{ suggestion.reason }}</p>
            <ul><li v-for="(field, name) in suggestion.fields" :key="name">{{ name }} → {{ field.choice }}：{{ field.reason }}</li></ul>
          </li>
        </ul>
      </div>
      <div v-if="!conflictCount" class="merge-clean"><strong>没有需要人工解决的冲突。</strong><span>确认后将把分支的新增内容和自动三方合并结果写入 main。</span></div>
      <article v-for="conflict in preview.conflicts" :key="keyOf(conflict)" class="merge-conflict">
        <header><strong>{{ conflict.title || (isEntity(conflict) ? '实体变更' : '关系变更') }}</strong><code>{{ conflict.kind }}</code></header>
        <div v-if="conflict.kind === 'duplicate_title'" class="merge-row"><label>同名实体处理<select v-model="decisions[keyOf(conflict)]"><option value="keep_both">保留两者</option><option value="skip_branch_entity">跳过分支版本</option></select></label></div>
        <template v-else-if="fieldsFor(conflict).length">
          <div v-for="field in fieldsFor(conflict)" :key="field" class="merge-field">
            <div class="merge-field-head"><strong>{{ field }}</strong><span>基线：{{ display(conflict.field_changes?.[field]?.base) }}</span></div>
            <div class="merge-values"><div><small>main</small><pre>{{ display(conflict.field_changes?.[field]?.main) }}</pre></div><div><small>branch</small><pre>{{ display(conflict.field_changes?.[field]?.branch) }}</pre></div></div>
            <select :value="choice(conflict, field)" @change="setChoice(conflict, field, ($event.target as HTMLSelectElement).value)"><option value="keep_main">采用 main</option><option value="keep_branch">采用 branch</option><option value="custom">编辑最终值</option></select>
            <textarea v-if="choice(conflict, field) === 'custom'" v-model="customValues[customKey(conflict, field)]" :placeholder="display(conflict.field_changes?.[field]?.main)" rows="3" />
          </div>
        </template>
        <div v-else class="merge-row"><label>冲突处理<select v-model="decisions[keyOf(conflict)]"><option value="keep_main">采用 main</option><option value="keep_branch">采用 branch</option></select></label></div>
      </article>
      <footer class="merge-footer"><button @click="cancel">取消</button><button class="primary" :disabled="submitting || suggestionLoading || stalePreview || staleSuggestions" @click="submit">{{ submitting ? '正在合并…' : '确认并合并到 main' }}</button></footer>
    </template>
  </section>
</template>
