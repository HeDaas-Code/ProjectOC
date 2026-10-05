<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useWorkbench } from './stores/workbench'
import { api } from './services/api'
import { type Canvas, type Workspace } from './types'
import CanvasSurface from './components/CanvasSurface.vue'
import DialoguePanel from './components/DialoguePanel.vue'
import ProposalPanel from './components/ProposalPanel.vue'
import GraphPanel from './components/GraphPanel.vue'
import HistoryPanel from './components/HistoryPanel.vue'
import TimelinePanel from './components/TimelinePanel.vue'
import MergePanel from './components/MergePanel.vue'
import AdminPanel from './components/AdminPanel.vue'

const store = useWorkbench()
const authUser = ref<any>(null)
const workspaceRole = ref<'owner' | 'editor' | 'reader'>('reader')
const ownerExists = ref(true)
const authName = ref('')
const authPassword = ref('')
const authEmail = ref('')
const authError = ref('')
const tab = ref('canvas')
const panel = ref('dialogue')
const search = ref('')
const showArchived = ref(false)
const selectedId = ref('')
const surface = ref<InstanceType<typeof CanvasSurface>>()
const loading = ref(true)
const mergeBranchId = ref('')
const inviteToken = ref(window.location.pathname.startsWith('/invite/') ? decodeURIComponent(window.location.pathname.slice('/invite/'.length).split('/')[0]) : '')
const inviteInfo = ref<any>(null)
const inviteName = ref('')
const invitePassword = ref('')
const inviteEmail = ref('')
const inviteError = ref('')
const inviteLoading = ref(false)

const entities = computed(() => store.entities.filter(e => e.title.toLowerCase().includes(search.value.toLowerCase())))
const canEditWorkspace = computed(() => workspaceRole.value === 'owner' || workspaceRole.value === 'editor')
const canManageWorkspace = computed(() => workspaceRole.value === 'owner')
const pending = computed(() => store.proposals.filter(p => p.status === 'pending').length)

async function run(fn: () => Promise<unknown>) { try { store.error = ''; await fn() } catch(e) { store.error = String(e) } }
async function loadWorkspaceRole() {
  workspaceRole.value = 'reader'
  if (!authUser.value || !store.workspace) return
  try {
    const members = await api<{ user: string; role: 'owner' | 'editor' | 'reader' }[]>(`members/?workspace=${store.workspace.id}`)
    const member = members.find(item => String(item.user) === String(authUser.value.id))
    workspaceRole.value = member?.role || 'reader'
  } catch { /* fail closed */ }
}
async function flush() { await surface.value?.flush() }
async function newWorld() { const name = prompt('为这个世界起一个名字'); if (!name?.trim()) return; await run(async () => { await flush(); const w = await api<Workspace>('workspaces/', 'POST', { name }); store.workspaces.unshift(w); await store.selectWorkspace(w); await newCanvas() }) }
async function newCanvas() { if (!store.workspace) return; await run(async () => { await flush(); const c = await api<Canvas>('canvases/', 'POST', { workspace: store.workspace!.id, name: '新的灵感画布' }); store.canvases.unshift(c); await store.selectCanvas(c); tab.value = 'canvas' }) }
async function chooseCanvas(c: Canvas) { if (store.sending) return; await run(async () => { await flush(); await store.selectCanvas(c); tab.value = 'canvas' }) }
async function workspaceChange(e: Event) { await run(async () => { await flush(); await store.selectWorkspace(store.workspaces.find(w => w.id === (e.target as HTMLSelectElement).value)!); selectedId.value = ''; tab.value = 'canvas' }) }
async function canvasAction(action: string) { if (!store.canvas) return; await run(async () => { await flush(); const c = store.canvas!; let result: Canvas
  if (action === 'duplicate') result = await api(`canvases/${c.id}/duplicate/`, 'POST', {})
  else if (action === 'restore') { const revisions = await api<{version:number}[]>(`canvases/${c.id}/revisions/`); const version = Number(prompt(`恢复快照版本，可选：${revisions.map(r => r.version).join(', ')}`)); if (!version) return; result = await api(`canvases/${c.id}/restore/`, 'POST', { version, expected_version: c.snapshot_version }) }
  else { const name = action === 'rename' ? prompt('画布名称', c.name) : c.name; if (!name) return; result = await api(`canvases/${c.id}/`, 'PATCH', { name, status: action === 'archive' ? 'archived' : action === 'unarchive' ? 'active' : c.status, expected_version: c.snapshot_version }) }
  store.canvases = await api(`canvases/?workspace=${store.workspace!.id}`); store.canvas = undefined; await new Promise(r => setTimeout(r,0)); await store.selectCanvas(result)
}) }
async function changeBranch(e: Event) { await run(() => store.selectBranch((e.target as HTMLSelectElement).value)); selectedId.value = '' }
async function createBranch() { if (!store.workspace) return; const name = prompt('新工作分支名称'); if (!name?.trim()) return; await run(async () => { const branch = await api('branches/', 'POST', { workspace: store.workspace!.id, name: name.trim() }); await store.refreshBranches(); await store.selectBranch(branch.id) }) }
async function mergeBranch() { const branch = store.branches.find(b => b.id === store.branchKey); if (!branch || branch.name === 'main' || branch.status !== 'active') return; mergeBranchId.value = branch.id }
async function finishMerge() { mergeBranchId.value = ''; await store.refreshBranches(); await store.selectBranch('main'); await store.refreshWorld() }
function focusTimelineTarget(target: { id: string; kind: string }) { selectedId.value = target.id; if (target.kind !== 'event') tab.value = 'graph' }
async function archiveBranch() { const branch = store.branches.find(b => b.id === store.branchKey); if (!branch || branch.name === 'main' || branch.status !== 'active' || !confirm(`归档分支“${branch.name}”？`)) return; await run(async () => { await api(`branches/${branch.id}/archive/`, 'POST', {}); await store.refreshBranches(); await store.selectBranch('main') }) }
async function changeTab(value: string) { await run(async () => { await flush(); tab.value = value }) }
async function focusMaintenanceTarget(id: string) { await run(async () => { try { await api(`entities/${id}/?workspace=${store.workspace!.id}${store.branchKey === 'main' ? '' : `&branch=${encodeURIComponent(store.branchKey)}`}`); selectedId.value = id } catch { const relation = await api<{ source: string }>(`relations/${id}/`); selectedId.value = relation.source } tab.value = 'graph' }) }
function saved(c: Canvas) { if (store.canvas?.id === c.id) { store.canvas = c; const i = store.canvases.findIndex(x => x.id === c.id); if (i >= 0) store.canvases[i] = c } }
async function authenticate(mode: 'login'|'bootstrap') { authError.value = ''; try { await api('auth/csrf/'); const result:any = await api(`auth/${mode}/`, 'POST', { username: authName.value, password: authPassword.value, email: authEmail.value }); authUser.value = result.user; await run(store.init) } catch(e) { authError.value = String(e) } finally { loading.value = false } }
async function loadInvite() { try { inviteInfo.value = await api(`auth/invites/${encodeURIComponent(inviteToken.value)}/`) } catch (e) { inviteError.value = String(e) } }
async function acceptInviteForExistingUser() { if (!authUser.value || !inviteToken.value) return; await api('auth/invites/accept/', 'POST', { token: inviteToken.value }); window.history.replaceState({}, '', '/'); inviteToken.value = ''; await run(store.init) }
async function authenticateInvite() { inviteError.value = ''; inviteLoading.value = true; try { await api('auth/csrf/'); const result:any = await api('auth/invites/signup/', 'POST', { token: inviteToken.value, username: inviteName.value, password: invitePassword.value, email: inviteEmail.value || inviteInfo.value?.email || '' }); authUser.value = result.user; window.history.replaceState({}, '', '/'); inviteToken.value = ''; await run(store.init) } catch(e) { inviteError.value = String(e) } finally { inviteLoading.value = false; loading.value = false } }
async function signOut() { await api('auth/logout/','POST',{}); authUser.value = null; workspaceRole.value = 'reader'; store.workspaces=[]; store.workspace=undefined }
onMounted(async () => {
  try {
    await api('auth/csrf/')
    if (inviteToken.value) {
      await loadInvite()
      try { authUser.value = await api('auth/me/'); await acceptInviteForExistingUser() } catch { /* guest invite signup below */ }
    } else {
      ownerExists.value = (await api<{owner_exists:boolean}>('auth/bootstrap/')).owner_exists
      authUser.value = await api('auth/me/')
      await run(store.init)
      await loadWorkspaceRole()
    }
  } catch { if (!inviteToken.value) { authUser.value = null; workspaceRole.value = 'reader' } }
  finally { loading.value = false }
})
watch(() => [store.workspace?.id, authUser.value?.id], () => { void loadWorkspaceRole() })
</script>

<template>
  <div v-if="inviteToken && !authUser" class="auth-screen">
    <form class="auth-card invite-auth-card" @submit.prevent="authenticateInvite">
      <div class="brand-mark">◈</div><p class="eyebrow">PROJECT OC · INVITATION</p>
      <template v-if="inviteInfo"><h1>加入「{{ inviteInfo.workspace_name }}」</h1><p>你被邀请以 <strong>{{ inviteInfo.role === 'editor' ? 'Editor · 可编辑' : 'Reader · 只读' }}</strong> 身份加入这个私人工作台。</p><label>用户名<input v-model="inviteName" required autocomplete="username" /></label><label>邮箱（可选）<input v-model="inviteEmail" :placeholder="inviteInfo.email || '用于找回身份'
" /></label><label>密码<input v-model="invitePassword" type="password" required minlength="12" autocomplete="new-password" /></label><p v-if="inviteError" class="error-banner">{{ inviteError }}</p><button class="primary" type="submit" :disabled="inviteLoading">{{ inviteLoading ? '加入中…' : '创建账户并加入' }}</button></template><p v-else class="error-banner">{{ inviteError || '邀请链接无效或已过期。' }}</p>
    </form>
  </div>
  <div v-else-if="!loading && !authUser" class="auth-screen"><form class="auth-card" @submit.prevent="authenticate(ownerExists ? 'login' : 'bootstrap')"><div class="brand-mark">◈</div><p class="eyebrow">PROJECT OC · PRIVATE WORKSPACE</p><h1>{{ ownerExists ? '登录工作台' : '创建首个 Owner' }}</h1><p>{{ ownerExists ? '此工作台仅对受邀成员开放。' : '首个账户将成为唯一初始所有者，之后关闭公开注册。' }}</p><label>用户名<input v-model="authName" required autocomplete="username" /></label><label v-if="!ownerExists">邮箱<input v-model="authEmail" type="email" autocomplete="email" /></label><label>密码<input v-model="authPassword" type="password" required :minlength="ownerExists ? 1 : 12" autocomplete="current-password" /></label><p v-if="authError" class="error-banner">{{ authError }}</p><button class="primary" type="submit">{{ ownerExists ? '登录' : '创建并进入' }}</button></form></div>
  <div v-else-if="authUser" class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">◈</span><div><strong>未定之书</strong><small>PROJECT OC · 世界观工作台</small></div></div>
      <div class="workspace-picker"><label class="eyebrow">当前世界</label><select :value="store.workspace?.id" @change="workspaceChange" :disabled="store.sending"><option v-if="!store.workspace" value="">选择或创建世界观</option><option v-for="w in store.workspaces" :key="w.id" :value="w.id">{{ w.name }}</option></select><button class="text-button" @click="newWorld" :disabled="store.sending">＋ 新建世界观</button></div>
      <nav><button :class="{ active: tab === 'canvas' }" @click="changeTab('canvas')">▧ 灵感暂存区 <small>{{ store.canvases.length }}</small></button><button :class="{ active: tab === 'graph' }" @click="changeTab('graph')">⌘ 世界观图谱</button><button :class="{ active: tab === 'timeline' }" @click="changeTab('timeline')">◷ 时间线</button><button :class="{ active: tab === 'history' }" @click="changeTab('history')">◷ 版本与维护</button><button v-if="canManageWorkspace && store.workspace" :class="{ active: tab === 'admin' }" @click="changeTab('admin')">⚙ 工作台管理</button></nav>
      <div class="section-label"><span>我的画布</span><button @click="newCanvas" :disabled="!store.workspace || store.sending" aria-label="新建画布">＋</button></div><div class="canvas-list"><button v-for="c in store.canvases.filter(c => showArchived || c.status !== 'archived')" :key="c.id" :class="{ selected: c.id === store.canvas?.id }" @click="chooseCanvas(c)"><span>▤</span> {{ c.name }} <small v-if="c.status === 'archived'">归档</small></button></div><label class="archive-toggle"><input v-model="showArchived" type="checkbox"/> 显示归档</label><div class="section-label">正式实体 <span>{{ store.entities.length }}</span></div><input v-model="search" placeholder="搜索设定、人物、物品…" aria-label="搜索实体"/><div class="entity-list"><button v-for="e in entities" :key="e.id" @click="selectedId = e.id; changeTab('graph')">◇ {{ e.title }}</button><p v-if="!entities.length" class="muted">确认后的设定会出现在这里</p></div><footer><span class="status-dot"/> {{ authUser.username }}<button class="text-button" @click="signOut">退出</button><small>先容纳灵感，再建立秩序。</small></footer>
    </aside>
    <main class="main">
      <header class="topbar"><div><span class="breadcrumb">{{ store.workspace?.name || '你的下一个世界' }} / {{ tab === 'canvas' ? '暂存区' : tab === 'graph' ? '图谱' : tab === 'timeline' ? '时间线' : tab === 'history' ? '版本' : '管理后台' }}</span><h1>{{ tab === 'canvas' ? store.canvas?.name || '让世界从这里生长' : tab === 'graph' ? '万物之间的联系' : tab === 'timeline' ? '角色与事件，沿着时间展开' : tab === 'history' ? '每一次选择，都有迹可循' : '管理你的私人工作台' }}</h1></div><div v-if="store.workspace && tab !== 'admin'" class="branch-controls"><label>工作分支 <select :value="store.branchKey" @change="changeBranch"><option value="main">main · 正式世界</option><option v-for="b in store.branches.filter(b => b.name !== 'main' && b.status === 'active')" :key="b.id" :value="b.id">{{ b.name }}</option></select></label><button @click="createBranch">＋ 分支</button><button v-if="store.branchKey !== 'main'" @click="mergeBranch">预览 / 合并</button><button v-if="store.branchKey !== 'main'" @click="archiveBranch">归档分支</button></div><div class="top-actions" v-if="store.canvas && tab === 'canvas'"><button @click="canvasAction('rename')">命名</button><button @click="canvasAction('duplicate')">复制</button><button @click="canvasAction('restore')">历史快照</button><button @click="canvasAction(store.canvas.status === 'archived' ? 'unarchive' : 'archive')">{{ store.canvas.status === 'archived' ? '取消归档' : '归档' }}</button><button class="primary" @click="panel = 'proposals'">审核提案 <span>{{ pending }}</span></button></div></header>
      <MergePanel v-if="mergeBranchId" :branch-id="mergeBranchId" :branch-name="store.branches.find(b => b.id === mergeBranchId)?.name || mergeBranchId" @merged="finishMerge" @cancel="mergeBranchId = ''" @error="store.error = $event" />
      <div v-if="store.error" class="error-banner" role="alert">{{ store.error }}<button @click="store.error = ''">关闭</button></div>
      <div v-if="loading" class="welcome"><h2>正在打开工作台…</h2></div><div v-else-if="!store.workspace" class="welcome"><span class="eyebrow">EVERY WORLD BEGINS WITH A QUESTION</span><h2>还没有名字的世界，<br/>也值得被认真记录。</h2><p>连接散落的灵感，推敲设定的逻辑，与 AI 一起寻找故事的可能。</p><button class="primary" @click="newWorld">创建第一个世界观 →</button></div>
      <div v-else-if="tab === 'admin' && canManageWorkspace" class="admin-page"><AdminPanel :workspace="store.workspace" :role="workspaceRole" @updated="store.workspace = $event" @error="store.error = $event" /></div>
      <div v-else-if="tab === 'canvas'" class="workbench"><div class="canvas-column"><CanvasSurface v-if="store.canvas" :key="store.canvas.id" ref="surface" :canvas="store.canvas" :proposals="store.proposals" @saved="saved" @error="store.error = $event"/><div v-else class="welcome"><h2>给灵感一片空白</h2><button class="primary" @click="newCanvas">新建灵感画布</button></div></div><aside class="right-panel"><div class="panel-tabs"><button :class="{ active: panel === 'dialogue' }" @click="panel = 'dialogue'">✧ 产婆式对话</button><button :class="{ active: panel === 'proposals' }" @click="panel = 'proposals'">提案审核 <small>{{ pending }}</small></button></div><DialoguePanel v-show="panel === 'dialogue'"/><ProposalPanel v-show="panel === 'proposals'"/></aside></div>
      <TimelinePanel v-else-if="tab === 'timeline'" :key="store.workspace.id" :workspace="store.workspace.id" :branch="store.branchKey" :entities="store.entities" @error="store.error = $event" @focus-target="focusTimelineTarget"/><GraphPanel v-else-if="tab === 'graph'" :key="store.workspace.id" :workspace="store.workspace.id" :branch="store.branchKey" :revision="store.jobs.map(j => j.id).join()" :selected-id="selectedId" @error="store.error = $event"/><HistoryPanel v-else :key="`${store.workspace.id}:${store.branchKey}:${store.canvas?.id || 'none'}`" :branch="store.branchKey" :canvas="store.canvas?.id" :can-edit="canEditWorkspace" @focus-target="focusMaintenanceTarget"/>
    </main>
  </div>
</template>
