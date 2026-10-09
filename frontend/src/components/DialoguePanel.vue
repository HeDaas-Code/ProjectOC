<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { renderMarkdown } from '../utils/markdown'
import { useWorkbench } from '../stores/workbench'
import { api } from '../services/api'
import type { AgentEvidence, AgentFinding, AgentRun } from '../types'

const store = useWorkbench()
const input = ref('')
const composerInput = ref<HTMLTextAreaElement>()
const questionNotice = ref('')
const questionActionBusy = ref(false)
const activeQuestion = computed(() => {
  const question = store.session?.context.question
  return question && ['pending', 'answering'].includes(question.status) ? question : null
})
const noteInput = ref('')
const auditOpen = ref(false)
const activeView = ref<'dialogue' | 'memory' | 'analysis' | 'tools'>('dialogue')
const copilotEvents = ref<any[]>([])
const copilotRuns = ref<any[]>([])
async function loadCopilotEvents() {
  if (!store.session) return
  try {
    const payload = await api<{ events: any[]; runs?: any[] }>(`dialogue/sessions/${store.session.id}/tools/`)
    copilotEvents.value = payload.events
    copilotRuns.value = payload.runs || []
  } catch (error) { store.error = String(error) }
}
const analysisMode = ref<'consistency' | 'timeline'>('consistency')
const analysisRun = ref<AgentRun>()
const analysisFindings = ref<AgentFinding[]>([])
const analysisEvidence = ref<AgentEvidence[]>([])
const analysisLoading = ref(false)
const analysisError = ref('')
const canAnalyze = computed(() => Boolean(store.workspace && store.session))
const showDialogueIntro = computed(() =>
  activeView.value === 'dialogue' &&
  !store.sending &&
  !store.session?.messages.some((message) => message.role === 'user'),
)

async function send() {
  const text = input.value.trim()
  if (!text) return

  const answeredQuestion = activeQuestion.value?.status === 'answering'
    ? activeQuestion.value.text
    : ''
  try {
    await store.send(text)
    input.value = ''
    if (answeredQuestion) {
      await store.updateMemory({ archive_question: { text: answeredQuestion, reason: '已回答' } })
    }
  } catch (error) {
    store.error = String(error)
  }
}

async function focusComposer() {
  activeView.value = 'dialogue'
  await nextTick()
  composerInput.value?.focus()
}

async function setQuestionStatus(status: 'answering' | 'skipped' | 'deferred' | 'pending') {
  const session = store.session
  const question = session?.context.question
  if (!session || !question) return

  try {
    const context = {
      ...session.context,
      question: { ...question, status },
    }
    store.session = await api(`dialogue/sessions/${session.id}/`, 'PATCH', { context })
    if (status === 'answering') {
      questionNotice.value = '已把这个问题放入输入框，写完后发送即可。'
      input.value = `关于“${question.text}”，我的想法是：`
      await focusComposer()
    } else if (status === 'pending') {
      questionNotice.value = '已恢复这个问题，你可以继续回答，也可以再次稍后处理。'
      await store.updateMemory({ restore_question: { text: question.text } })
      input.value = `关于“${question.text}”，我想继续思考：`
      await focusComposer()
    } else if (status === 'skipped') {
      questionNotice.value = '已跳过这个问题；它仍保留在长期记忆中，可以稍后恢复。'
      await store.updateMemory({ archive_question: { text: question.text, reason: '用户选择跳过，可在长期记忆中恢复' } })
    } else {
      questionNotice.value = '已稍后处理；问题已保留在长期记忆中，不会阻止继续创作。'
      // Deferred questions remain in branch memory, but leave the active prompt area.
      await store.refreshMemory()
    }
  } catch (error) {
    store.error = String(error)
  }
}

async function questionAction(action: 'answer' | 'skip' | 'defer') {
  if (!activeQuestion.value || questionActionBusy.value) return
  questionActionBusy.value = true
  try {
    if (action === 'answer') await setQuestionStatus('answering')
    else if (action === 'skip') await setQuestionStatus('skipped')
    else await setQuestionStatus('deferred')
  } finally {
    questionActionBusy.value = false
  }
}

async function resumeQuestion(text: string) {
  const session = store.session
  if (!session || !session.context.question || session.context.question.text !== text) {
    store.error = '此问题来自其他对话。请打开生成该问题的画布后继续处理。'
    return
  }
  await setQuestionStatus('pending')
}

async function saveNote() {
  const text = noteInput.value.trim()
  if (!text) return

  try {
    await store.updateMemory({
      manual_notes: [...(store.memory?.manual_notes || []), { text }],
    })
    noteInput.value = ''
  } catch (error) {
    store.error = String(error)
  }
}

async function archiveQuestion(text: string) {
  try {
    await store.updateMemory({ archive_question: { text, reason: '用户在记忆面板中归档' } })
  } catch (error) {
    store.error = String(error)
  }
}

async function toggleAudit() {
  auditOpen.value = !auditOpen.value
  if (auditOpen.value) await store.refreshMemory(true)
}

async function runAnalysis() {
  if (!store.session || !canAnalyze.value || analysisLoading.value) return

  analysisLoading.value = true
  analysisError.value = ''

  try {
    const response = await api<{
      run_id: string
      status: string
      result?: { findings?: AgentFinding[]; evidence?: AgentEvidence[] }
    }>(`dialogue/sessions/${store.session.id}/analysis/`, 'POST', {
      mode: analysisMode.value,
      branch: store.branchKey,
      context_revision: store.memory?.revision || 0,
      token_budget: 6000,
      model: store.model,
    })
    analysisFindings.value = response.result?.findings || []
    analysisEvidence.value = response.result?.evidence || []
    analysisRun.value = await api<AgentRun>(`agent/runs/${response.run_id}/`)
  } catch (error) {
    analysisError.value = String(error)
  } finally {
    analysisLoading.value = false
  }
}

function evidenceFor(finding: AgentFinding) {
  return finding.evidence?.length
    ? finding.evidence
    : analysisEvidence.value.filter((item) => finding.explanation.includes(item.excerpt))
}

function askFinding(finding: AgentFinding) {
  const question = finding.suggested_questions?.[0]
  if (!question) return
  input.value = question
  activeView.value = 'dialogue'
}

function askEvidence(evidence: AgentEvidence) {
  input.value = `请查看证据 ${evidence.source_type}:${evidence.source_id} ${evidence.field_path}`
  activeView.value = 'dialogue'
}
</script>

<template>
  <div class="dialogue-panel">
    <nav class="dialogue-tabs" aria-label="对话工作区">
      <button
        type="button"
        :class="{ active: activeView === 'dialogue' }"
        :aria-selected="activeView === 'dialogue'"
        @click="activeView = 'dialogue'"
      >
        产婆对话
      </button>
      <button
        type="button"
        :class="{ active: activeView === 'memory' }"
        :aria-selected="activeView === 'memory'"
        @click="activeView = 'memory'"
      >
        长期记忆
      </button>
      <button
        type="button"
        :class="{ active: activeView === 'analysis' }"
        :aria-selected="activeView === 'analysis'"
        @click="activeView = 'analysis'"
      >
        Agent 分析
      </button>
      <button type="button" :class="{ active: activeView === 'tools' }" :aria-selected="activeView === 'tools'" @click="activeView = 'tools'; loadCopilotEvents()">
        工具记录 <small v-if="copilotEvents.length">{{ copilotEvents.length }}</small>
      </button>
    </nav>

    <div v-if="showDialogueIntro" class="panel-heading dialogue-intro">
      <span class="eyebrow">MAIEUTIC DIALOGUE</span>
      <h2>把灵感问得更深一点</h2>
      <p>AI 提出可能性，你决定什么成为真实。</p>
    </div>

    <section v-if="activeView === 'dialogue'" class="dialogue-view" aria-label="产婆式对话">
      <div class="messages" aria-live="polite">
        <div v-if="!store.session?.messages.length" class="empty-state">
          <span>✧</span>
          <h3>世界从一个问题开始</h3>
          <p>描述一条规则、一件物品，或一个尚未想清楚的念头。这里的一切都只是草稿。</p>
          <button @click="input = '创建一个星辰魔法设定，力量来自星光；游离设定：银制物品会储存星光。'">
            试试：星辰魔法与银制物品 ↗
          </button>
        </div>
        <article
          v-for="message in store.session?.messages"
          :key="message.id"
          :class="['message', message.role]"
        >
          <small>{{ message.role === 'user' ? '你' : '构建伙伴' }}</small>
          <div class="message-markdown" v-html="renderMarkdown(message.content)" />
        </article>
        <article v-if="store.streamText" class="message assistant">
          <small>构建伙伴 · 正在思考</small>
          <div class="message-markdown" v-html="renderMarkdown(store.streamText)" />
        </article>
      </div>

      <div v-if="activeQuestion" class="question">
        <small>下一步 · {{ activeQuestion.status === 'answering' ? '等待你的回答' : '待处理' }}</small>
        <p>{{ activeQuestion.text }}</p>
        <details>
          <summary>为什么问这个问题？</summary>
          <p>{{ activeQuestion.reason }}</p>
        </details>
        <div class="actions" aria-label="问题处理方式">
          <button type="button" :disabled="questionActionBusy" @click="questionAction('answer')">
            {{ questionActionBusy ? '处理中…' : '回答' }}
          </button>
          <button type="button" :disabled="questionActionBusy" @click="questionAction('skip')">跳过并归档</button>
          <button type="button" :disabled="questionActionBusy" @click="questionAction('defer')">稍后处理</button>
        </div>
      </div>

      <p v-if="questionNotice" class="question-notice" role="status">{{ questionNotice }}</p>

      <div class="model-row composer-model-row">
        <select v-model="store.model" aria-label="选择模型" :disabled="store.sending">
          <option v-for="model in store.models" :key="model.id" :value="model.id">{{ model.id }}</option>
        </select>
        <small>{{ store.mode }}</small>
      </div>

      <form class="composer" @submit.prevent="send">
        <textarea
          ref="composerInput"
          v-model="input"
          rows="3"
          aria-label="描述世界观"
          placeholder="例如：这个世界的魔法来自记忆，但使用它会…"
          :disabled="store.sending"
        />
        <div>
          <small>不会自动写入正式设定</small>
          <button
            class="primary"
            :disabled="store.sending || !input.trim() || !store.session"
          >
            {{ store.sending ? '思考中…' : '一起构建 ↑' }}
          </button>
        </div>
      </form>
    </section>

    <section v-else-if="activeView === 'tools'" class="copilot-events" aria-label="副驾驶工具记录">
      <div class="panel-heading"><span class="eyebrow">COPILOT AUDIT</span><h2>副驾驶工具记录</h2><p>工具只能创建待审核草稿，不能直接修改正式实体。</p></div>
      <article v-for="run in copilotRuns" :key="`run-${run.id}`" class="copilot-event copilot-run">
        <header><strong>运行 {{ run.id.slice(0, 8) }}</strong><small>{{ new Date(run.created_at).toLocaleString() }}</small></header>
        <span :class="`run-status run-${run.status}`">{{ run.status }}</span>
        <small v-if="run.error_code">错误：{{ run.error_code }}</small>
        <ul v-if="run.tool_calls?.length"><li v-for="call in run.tool_calls" :key="`${run.id}-${call.tool_name}`">{{ call.tool_name }} · {{ call.status }} · {{ call.latency_ms }}ms</li></ul>
      </article>
      <article v-for="event in copilotEvents" :key="event.id" class="copilot-event"><header><strong>{{ event.tool }}</strong><small>{{ new Date(event.created_at).toLocaleString() }}</small></header><span>{{ event.status }}</span><pre>{{ JSON.stringify(event.arguments, null, 2) }}</pre></article>
      <p v-if="!copilotEvents.length && !copilotRuns.length" class="muted">暂无工具调用记录。</p>
    </section>

    <section
      v-else-if="activeView === 'memory'"
      class="dialogue-memory memory-view"
      aria-label="世界观长期记忆"
    >
      <template v-if="store.memory">
        <div class="dialogue-memory-heading">
          <strong>长期记忆 · r{{ store.memory.revision }}</strong>
          <small>
            {{ store.memory.confirmed_facts.filter((fact) => fact.kind === 'entity').length }} 个正式实体 ·
            {{ store.memory.working_notes.length }} 个待审核草稿
          </small>
        </div>
        <p>{{ store.memory.summary }}</p>
        <details>
          <summary>查看记忆边界</summary>
          <p>正式事实来自当前分支；草稿和问题只作为工作上下文，不会自动写入世界观。</p>
          <pre v-if="store.memory.recent_session_summary">{{ store.memory.recent_session_summary }}</pre>
        </details>
        <div class="memory-editor">
          <div class="memory-editor-heading">
            <strong>我的工作笔记</strong>
            <small>只属于当前工作区 / 分支</small>
          </div>
          <div v-if="(store.memory.manual_notes || []).length" class="memory-notes">
            <div
              v-for="note in (store.memory.manual_notes || [])"
              :key="note.updated_at + note.text"
              class="memory-note"
            >
              {{ note.text }}
            </div>
          </div>
          <div class="memory-note-form">
            <input
              v-model="noteInput"
              aria-label="新增工作笔记"
              placeholder="记下一条不属于正式设定的提醒…"
              @keyup.enter="saveNote"
            />
            <button @click="saveNote" :disabled="!noteInput.trim()">记录</button>
          </div>
          <div v-if="store.memory.open_questions.length" class="memory-questions">
            <small>未阻塞问题</small>
            <div
              v-for="question in store.memory.open_questions"
              :key="String(question.text)"
              class="memory-question"
            >
              <span>{{ question.text }}</span>
              <span class="memory-question-actions">
                <button
                  v-if="store.session?.context.question?.text === question.text"
                  @click="resumeQuestion(String(question.text))"
                >继续处理</button>
                <button @click="archiveQuestion(String(question.text))">归档</button>
              </span>
            </div>
          </div>
          <div v-if="(store.memory.archived_questions || []).length" class="memory-questions archived">
            <small>已归档问题</small>
            <div
              v-for="question in (store.memory.archived_questions || [])"
              :key="String(question.text)"
              class="memory-question"
            >
              <span>{{ question.text }}</span>
              <button @click="resumeQuestion(String(question.text))">重新处理</button>
            </div>
          </div>
          <button class="text-button" @click="toggleAudit">
            {{ auditOpen ? '收起记忆变更记录' : '查看记忆变更记录' }}
          </button>
          <div v-if="auditOpen" class="memory-audit">
            <div v-for="entry in (store.memory.audit || [])" :key="entry.id">
              <small>{{ new Date(entry.created_at).toLocaleString() }} · {{ entry.action }}</small>
            </div>
          </div>
        </div>
      </template>
      <div v-else class="tab-empty-state">
        <strong>暂无长期记忆</strong>
        <p>打开一个对话后，这里会显示当前工作区与分支的长期上下文。</p>
      </div>
    </section>

    <section
      v-else
      class="agent-analysis analysis-view"
      aria-label="设定一致性与时间线分析"
    >
      <div class="analysis-heading">
        <div>
          <strong>高级 Agent 检查</strong>
          <small>只读分析 · 结论带证据 · 不会直接写入正式设定</small>
        </div>
        <div class="analysis-actions">
          <select v-model="analysisMode" :disabled="analysisLoading">
            <option value="consistency">设定一致性</option>
            <option value="timeline">时间线推理</option>
          </select>
          <button type="button" @click="runAnalysis" :disabled="!canAnalyze || analysisLoading">
            {{ analysisLoading ? '分析中…' : '运行分析' }}
          </button>
        </div>
      </div>
      <p v-if="analysisError" class="analysis-error">{{ analysisError }}</p>
      <div v-if="analysisRun" class="analysis-meta">
        <span>{{ analysisRun.status }}</span>
        <span v-if="analysisRun.output_tokens">{{ analysisRun.output_tokens }} tokens</span>
        <span v-if="analysisRun.latency_ms">{{ analysisRun.latency_ms }} ms</span>
        <span>分支：{{ analysisRun.branch }}</span>
      </div>
      <div v-if="analysisFindings.length" class="analysis-findings">
        <article
          v-for="finding in analysisFindings"
          :key="finding.title + finding.explanation"
          class="analysis-finding"
          :class="`severity-${finding.severity}`"
        >
          <div class="finding-title">
            <span>{{ finding.severity === 'error' ? '⛔' : finding.severity === 'warning' ? '⚠' : 'ℹ' }}</span>
            <strong>{{ finding.title }}</strong>
            <small>{{ finding.category }} · {{ Math.round(finding.confidence * 100) }}%</small>
          </div>
          <p>{{ finding.explanation }}</p>
          <div v-if="evidenceFor(finding).length" class="finding-evidence">
            <small>证据</small>
            <button
              v-for="evidence in evidenceFor(finding)"
              :key="`${evidence.source_type}:${evidence.source_id}:${evidence.field_path}`"
              type="button"
              @click="askEvidence(evidence)"
            >
              {{ evidence.source_type }} · {{ evidence.source_id.slice(0, 8) }}
              <span v-if="evidence.field_path"> · {{ evidence.field_path }}</span>
            </button>
          </div>
          <button
            v-if="finding.suggested_questions?.length"
            type="button"
            class="text-button"
            @click="askFinding(finding)"
          >
            带着问题继续对话 ↗
          </button>
        </article>
      </div>
      <p v-else-if="analysisRun?.status === 'completed'" class="analysis-empty">
        没有发现需要处理的问题。你可以继续深化或运行另一种分析。
      </p>
      <div v-else class="tab-empty-state">
        <strong>还没有分析结果</strong>
        <p>选择一种检查方式，Agent 会先读取当前分支上下文，再给出带证据的只读结论。</p>
      </div>
    </section>
  </div>
</template>
