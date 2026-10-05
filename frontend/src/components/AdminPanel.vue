<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../services/api'
import type { Invite, Membership, Workspace } from '../types'

const props = defineProps<{ workspace: Workspace; role: 'owner' | 'editor' | 'reader' }>()
const emit = defineEmits<{ (event: 'updated', workspace: Workspace): void; (event: 'error', message: string): void }>()

const loading = ref(false)
const saving = ref(false)
const inviteLoading = ref(false)
const members = ref<Membership[]>([])
const invites = ref<Invite[]>([])
const lastLink = ref('')
const copied = ref(false)
const form = reactive({ name: '', description: '', provider: '', model: '', base_url: '', default_branch: 'main', api_key: '', api_key_configured: false, api_key_last4: '' })
const inviteForm = reactive({ role: 'reader' as 'reader' | 'editor', email: '', expires_days: 7 })
const canManage = computed(() => props.role === 'owner')

function hydrate(workspace: Workspace) {
  form.name = workspace.name || ''
  form.description = workspace.description || ''
  const ai = (workspace.settings?.ai || {}) as Record<string, unknown>
  form.provider = String(ai.provider || 'offline')
  form.model = String(ai.model || '')
  form.base_url = String(ai.base_url || '')
  form.api_key = ''
  form.api_key_configured = Boolean(workspace.ai_api_key_configured)
  form.api_key_last4 = String(workspace.ai_api_key_last4 || '')
  form.default_branch = String(workspace.settings?.default_branch || 'main')
}
async function load() {
  loading.value = true
  try {
    hydrate(props.workspace)
    members.value = await api<Membership[]>(`members/?workspace=${props.workspace.id}`)
    if (canManage.value) invites.value = await api<Invite[]>(`members/invites/?workspace=${props.workspace.id}`)
  } catch (error) { emit('error', String(error)) } finally { loading.value = false }
}
async function saveSettings() {
  if (!canManage.value) return
  saving.value = true
  try {
    const workspace = await api<Workspace>(`workspaces/${props.workspace.id}/`, 'PATCH', {
      name: form.name.trim(), description: form.description,
      settings: { ...(props.workspace.settings || {}), default_branch: form.default_branch || 'main', ai: { provider: form.provider, model: form.model, base_url: form.base_url } },
      ...(form.api_key ? { ai_api_key: form.api_key } : {}),
    })
    hydrate(workspace); form.api_key = ''; emit('updated', workspace)
  } catch (error) { emit('error', String(error)) } finally { saving.value = false }
}
async function clearApiKey() {
  if (!canManage.value || !confirm('清除这个工作台的 API Key？')) return
  saving.value = true
  try {
    const workspace = await api<Workspace>(`workspaces/${props.workspace.id}/`, 'PATCH', { ai_api_key: '' })
    hydrate(workspace); emit('updated', workspace)
  } catch (error) { emit('error', String(error)) } finally { saving.value = false }
}
async function createInvite() {
  if (!canManage.value) return
  inviteLoading.value = true; lastLink.value = ''; copied.value = false
  try {
    const invite = await api<Invite & { invite_url: string }>('members/', 'POST', { workspace: props.workspace.id, role: inviteForm.role, email: inviteForm.email.trim(), expires_days: inviteForm.expires_days })
    lastLink.value = new URL(invite.invite_url, window.location.origin).toString()
    invites.value = [invite, ...invites.value]
    inviteForm.email = ''
  } catch (error) { emit('error', String(error)) } finally { inviteLoading.value = false }
}
async function copyLink() {
  if (!lastLink.value) return
  await navigator.clipboard?.writeText(lastLink.value)
  copied.value = true
  window.setTimeout(() => copied.value = false, 1800)
}
async function updateRole(member: Membership, event: Event) {
  const role = (event.target as HTMLSelectElement).value
  try { const updated = await api<Membership>(`members/${props.workspace.id}/${member.user}/`, 'PATCH', { role }); members.value = members.value.map(item => item.user === updated.user ? updated : item) }
  catch (error) { emit('error', String(error)); await load() }
}
async function removeMember(member: Membership) {
  if (!confirm(`移除 ${member.username}？`)) return
  try { await api(`members/${props.workspace.id}/${member.user}/`, 'DELETE'); members.value = members.value.filter(item => item.user !== member.user) }
  catch (error) { emit('error', String(error)) }
}
async function revokeInvite(invite: Invite) {
  if (!confirm('撤销这条邀请链接？')) return
  try { await api(`members/invites/${invite.id}/`, 'DELETE'); invites.value = invites.value.filter(item => item.id !== invite.id) }
  catch (error) { emit('error', String(error)) }
}
watch(() => props.workspace.id, load)
onMounted(load)
</script>

<template>
  <section class="admin-panel">
    <div class="admin-heading">
      <div><span class="eyebrow">PRIVATE WORKSPACE CONTROL</span><h2>工作台管理后台</h2><p>这里管理世界观的基本信息、AI 配置和成员访问。工作台默认先服务于个人，其他人只能通过邀请链接加入。</p></div>
      <span class="admin-role">{{ role }} · {{ canManage ? '可管理' : '只读' }}</span>
    </div>
    <div v-if="loading" class="welcome admin-loading">正在读取工作台配置…</div>
    <template v-else>
      <div class="admin-grid">
        <section class="admin-card admin-settings-card">
          <div class="admin-card-heading"><div><h3>工作台配置</h3><p>API Key 会在后端加密保存，不会回传到浏览器，也不会写入 Git 世界观仓库。</p></div><button class="primary" :disabled="!canManage || saving" @click="saveSettings">{{ saving ? '保存中…' : '保存配置' }}</button></div>
          <label>工作台名称<input v-model="form.name" :disabled="!canManage" /></label>
          <label>简介<textarea v-model="form.description" :disabled="!canManage" rows="3" placeholder="这个世界观想探索什么？" /></label>
          <div class="admin-form-row"><label>AI 模式<select v-model="form.provider" :disabled="!canManage"><option value="offline">离线规则演示</option><option value="openai-compatible">OpenAI-compatible</option></select></label><label>默认模型<input v-model="form.model" :disabled="!canManage" placeholder="由后端 provider 提供" /></label></div>
          <label>OpenAI-compatible Base URL<input v-model="form.base_url" :disabled="!canManage" placeholder="留空则使用服务器配置" /></label>
          <label>OpenAI-compatible API Key<input v-model="form.api_key" :disabled="!canManage" type="password" autocomplete="new-password" placeholder="留空表示不修改当前 Key" /></label>
          <div v-if="form.api_key_configured" class="credential-status"><span>已配置 API Key（末四位：{{ form.api_key_last4 }}）</span><button class="text-button danger-text" :disabled="!canManage || saving" @click="clearApiKey">清除</button></div>
          <p v-else class="muted credential-help">尚未配置工作台 API Key，将使用服务器环境变量或离线模式。</p>
          <div class="admin-policy"><strong>访问策略：邀请制</strong><span>首个 Owner 创建后公开注册永久关闭；成员只能使用有效邀请链接加入。</span></div>
        </section>

        <section class="admin-card">
          <div class="admin-card-heading"><div><h3>生成邀请链接</h3><p>可以不绑定邮箱，复制链接发给朋友；链接使用一次后失效。</p></div></div>
          <div class="admin-form-row"><label>加入角色<select v-model="inviteForm.role" :disabled="!canManage"><option value="reader">Reader · 只读</option><option value="editor">Editor · 可编辑</option></select></label><label>有效期<select v-model.number="inviteForm.expires_days" :disabled="!canManage"><option :value="1">1 天</option><option :value="7">7 天</option><option :value="14">14 天</option><option :value="30">30 天</option></select></label></div>
          <label>限定邮箱（可选）<input v-model="inviteForm.email" :disabled="!canManage" placeholder="留空即为普通分享链接" type="email" /></label>
          <button class="primary full-button" :disabled="!canManage || inviteLoading" @click="createInvite">{{ inviteLoading ? '生成中…' : '生成邀请链接' }}</button>
          <div v-if="lastLink" class="invite-result"><small>邀请链接已生成，请立即复制：</small><code>{{ lastLink }}</code><button @click="copyLink">{{ copied ? '已复制' : '复制链接' }}</button></div>
        </section>
      </div>

      <section class="admin-card admin-members-card">
        <div class="admin-card-heading"><div><h3>成员与权限</h3><p>Owner 可以调整成员角色或移除成员。Reader 无法修改画布，Editor 可以参与创作。</p></div><span class="admin-count">{{ members.length }} 位成员</span></div>
        <div class="admin-table"><div v-for="member in members" :key="member.id" class="admin-row"><div><strong>{{ member.username }}</strong><small>{{ member.email || '未设置邮箱' }}</small></div><select :value="member.role" :disabled="!canManage || member.role === 'owner' && members.filter(item => item.role === 'owner').length === 1" @change="updateRole(member, $event)"><option value="owner">Owner</option><option value="editor">Editor</option><option value="reader">Reader</option></select><button v-if="member.role !== 'owner'" class="text-button danger-text" :disabled="!canManage" @click="removeMember(member)">移除</button></div><p v-if="!members.length" class="muted">暂无成员</p></div>
      </section>

      <section v-if="canManage" class="admin-card admin-invites-card"><div class="admin-card-heading"><div><h3>邀请记录</h3><p>已接受的链接保留历史记录，未接受的链接可以撤销。</p></div></div><div class="admin-table"><div v-for="invite in invites" :key="invite.id" class="admin-row"><div><strong>{{ invite.email || '分享链接' }} · {{ invite.role }}</strong><small>有效至 {{ new Date(invite.expires_at).toLocaleString() }}<span v-if="invite.accepted_at"> · 已接受</span></small></div><button v-if="!invite.accepted_at" class="text-button danger-text" @click="revokeInvite(invite)">撤销</button><span v-else class="invite-status">已使用</span></div><p v-if="!invites.length" class="muted">还没有生成邀请链接</p></div></section>
    </template>
  </section>
</template>
