import { ref } from 'vue'
import { api, ApiError } from './api'

export type MergeConflict = {
  branch_entity?: string
  branch_relation?: string
  title?: string
  kind: string
  conflicting_fields?: string[]
}
export type MergeSuggestion = {
  conflict_id: string
  choice: 'keep_main' | 'keep_branch' | 'keep_both' | 'skip_branch_entity'
  reason: string
  confidence: number
  fields: Record<string, { choice: 'keep_main' | 'keep_branch'; reason: string; confidence?: number }>
}
type Context = { branch: string; preview_token: string; conflicts: MergeConflict[] }

// Mapping a row suggestion to conflict fields preserves automatically merged,
// non-conflicting fields rather than replacing them with one whole side.
export function suggestionResolution(suggestion: MergeSuggestion, conflict: MergeConflict) {
  const fields = conflict.conflicting_fields || []
  if (!fields.length || conflict.kind === 'duplicate_title') return suggestion.choice
  return { fields: Object.fromEntries(fields.map(field => [field, suggestion.fields[field]?.choice || suggestion.choice])) }
}

export function useMergeSuggestions(context: () => Context | undefined) {
  const suggestions = ref<MergeSuggestion[]>([])
  const loading = ref(false)
  const error = ref('')
  const applied = ref(false)
  const stale = ref(false)
  let generation = 0
  let suggestedToken = ''
  function reset() {
    generation++
    suggestions.value = []
    suggestedToken = ''
    loading.value = false
    error.value = ''
    applied.value = false
    stale.value = false
  }
  async function request(model = '') {
    const current = context()
    if (!current?.conflicts.length || loading.value || stale.value) return
    const id = ++generation
    suggestions.value = []
    applied.value = false
    loading.value = true
    error.value = ''
    try {
      const result = await api<{ suggestions: MergeSuggestion[]; preview_token: string }>(
        `branches/${current.branch}/merge-suggestion/`, 'POST',
        { preview_token: current.preview_token, model },
      )
      if (id !== generation) return
      if (context()?.branch !== current.branch || context()?.preview_token !== result.preview_token || result.preview_token !== current.preview_token) {
        stale.value = true
        error.value = '建议对应的预览已变化，请重新读取差异。'
        return
      }
      suggestions.value = result.suggestions
      suggestedToken = result.preview_token
      if (!result.suggestions.length) error.value = 'AI 没有给出建议，可继续人工审核。'
    } catch (exception) {
      if (id !== generation) return
      stale.value = exception instanceof ApiError && exception.status === 409
      error.value = `AI 建议暂不可用：${String(exception)}`
    } finally {
      if (id === generation) loading.value = false
    }
  }
  function apply(decisions: Record<string, unknown>) {
    const current = context()
    if (!current || current.preview_token !== suggestedToken || stale.value || loading.value || applied.value) return
    for (const suggestion of suggestions.value) {
      const conflict = current.conflicts.find(c => (c.branch_entity || c.branch_relation) === suggestion.conflict_id)
      if (conflict) decisions[suggestion.conflict_id] = suggestionResolution(suggestion, conflict)
    }
    applied.value = true
  }
  return { suggestions, loading, error, applied, stale, reset, request, apply }
}
