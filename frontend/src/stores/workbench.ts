import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ApiError, api, consumeSSE, csrfHeader } from '../services/api'
import type { Workspace, Branch, Canvas, Proposal, RelationProposal, Entity, Session, DialogueMemory, Job } from '../types'
export const useWorkbench = defineStore('workbench', () => {
  const workspaces = ref<Workspace[]>([]), workspace = ref<Workspace>(), canvases = ref<Canvas[]>([]), canvas = ref<Canvas>()
  const proposals = ref<Proposal[]>([]), relations = ref<RelationProposal[]>([]), entities = ref<Entity[]>([]), session = ref<Session>(), memory = ref<DialogueMemory>()
  const branches = ref<Branch[]>([]), branchKey = ref("main")
  const models = ref<{ id: string }[]>([]), jobs = ref<Job[]>([]), model = ref(''), error = ref(''), sending = ref(false), mode = ref(''), streamText = ref('')
  async function init() {
    workspaces.value = await api('workspaces/')
    if (workspaces.value.length) await selectWorkspace(workspaces.value.find(w => w.id === localStorage.getItem('oc:workspace')) || workspaces.value[0])
    const response = await api(`ai/providers/default/models/${workspace.value ? `?workspace=${encodeURIComponent(workspace.value.id)}` : ''}`); models.value = response.models; mode.value = response.source === 'upstream' ? '已连接模型服务' : '离线规则演示 / 上游不可用'
    model.value = session.value?.model || models.value[0]?.id || ''
  }
  async function selectWorkspace(w: Workspace) {
    workspace.value = w; localStorage.setItem('oc:workspace', w.id); canvas.value = undefined; session.value = undefined; memory.value = undefined; proposals.value = []; relations.value = []
    canvases.value = await api(`canvases/?workspace=${w.id}`)
    branches.value = await api(`branches/?workspace=${w.id}`)
    branchKey.value = "main"
    await refreshWorld()
    const first = canvases.value.find(c => c.status !== 'archived'); if (first) await selectCanvas(first)
  }
  async function refreshWorld() {
    if (!workspace.value) return
    const branchParam = branchKey.value === "main" ? "" : `&branch=${encodeURIComponent(branchKey.value)}`
    entities.value = await api(`entities/?workspace=${workspace.value.id}${branchParam}`)
    jobs.value = await api(`commit-jobs/?workspace=${workspace.value.id}`)
  }
  async function selectCanvas(c: Canvas) {
    canvas.value = await api(`canvases/${c.id}/`)
    if (canvas.value) branchKey.value = canvas.value.branch || "main"
    await refreshWorld()
    await refreshProposals()
    const sessions = await api<Session[]>(`dialogue/sessions/?canvas=${c.id}`)
    session.value = sessions[0] || await api('dialogue/sessions/', 'POST', { workspace: c.workspace, canvas: c.id, model: model.value })
    model.value = session.value!.model || model.value
    await refreshMemory()
  }
  async function refreshMemory(withAudit = false) {
    if (!session.value) return
    memory.value = await api<DialogueMemory>(`dialogue/sessions/${session.value.id}/memory/${withAudit ? '?audit=1' : ''}`)
  }
  async function updateMemory(data: Record<string, unknown>) {
    if (!session.value || !memory.value) return
    try {
      memory.value = await api<DialogueMemory>(`dialogue/sessions/${session.value.id}/memory/`, 'PATCH', { expected_revision: memory.value.revision, ...data })
    } catch (error) {
      // A second session may have edited memory between render and click. Do
      // not discard the server's latest notes/questions; refresh first, then
      // surface the conflict so the user can retry deliberately.
      if (error instanceof ApiError && error.status === 409) await refreshMemory()
      throw error
    }
  }
  async function refreshProposals() {
    if (!canvas.value) return
    proposals.value = await api(`canvases/${canvas.value.id}/proposals/`)
    relations.value = await api(`relation-proposals/?canvas=${canvas.value.id}`)
  }
  async function send(text: string) {
    if (!session.value || sending.value) return
    const id = session.value.id; sending.value = true; streamText.value = ''; error.value = ''
    try {
      await api(`dialogue/sessions/${id}/`, 'PATCH', { model: model.value })
      const response = await fetch(`/api/v1/dialogue/sessions/${id}/messages/`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': decodeURIComponent(csrfHeader()) }, credentials: 'same-origin', body: JSON.stringify({ content: text }) })
      if (!response.ok || !response.body) throw new Error(await response.text())
      await consumeSSE(response.body, (event, data) => {
        if (event === 'token') streamText.value += data.content
        if (event === 'error') throw new Error(data.detail)
      })
    } finally {
      try { session.value = await api(`dialogue/sessions/${id}/`); await refreshProposals(); await refreshMemory() }
      finally { sending.value = false; streamText.value = '' }
    }
  }
  async function refreshBranches() {
    if (workspace.value) branches.value = await api(`branches/?workspace=${workspace.value.id}`)
  }
  async function selectBranch(key: string) {
    branchKey.value = key || "main"
    await refreshWorld()
  }
  return { workspaces, workspace, canvases, canvas, proposals, relations, entities, session, memory, jobs, branches, branchKey, models, model, error, sending, mode, streamText, init, selectWorkspace, selectCanvas, selectBranch, refreshBranches, refreshWorld, refreshProposals, refreshMemory, updateMemory, send }
})
