<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../services/api'

type TimeSystem = {
  id: string
  name: string
  epoch_label: string
  unit_name: string
  units: { name: string; ticks: number }[]
  calendar_rules?: Record<string, unknown>
  display_format?: string
}
type Conversion = {
  id: string
  source_system: string
  source_system_name: string
  target_system: string
  target_system_name: string
  numerator: number
  denominator: number
  offset: number
  label: string
}

const props = defineProps<{ workspace: string; systems: TimeSystem[]; selectedSystem: string }>()
const emit = defineEmits<{ updated: []; select: [id: string]; error: [message: string] }>()

const expanded = ref(false)
const editing = ref(false)
const saving = ref(false)
const conversions = ref<Conversion[]>([])
const activeId = ref('')
const name = ref('')
const epochLabel = ref('')
const unitName = ref('tick')
const displayFormat = ref('{value} {unit}')
const unitsJson = ref('[]')
const calendarRulesJson = ref('{}')
const sourceSystem = ref('')
const targetSystem = ref('')
const numerator = ref(1)
const denominator = ref(1)
const offset = ref(0)
const conversionLabel = ref('')
const editingConversionId = ref('')

const selected = computed(() => props.systems.find(item => item.id === props.selectedSystem) || props.systems[0])
const canCreateConversion = computed(() => sourceSystem.value && targetSystem.value && sourceSystem.value !== targetSystem.value)

function resetSystemForm(system?: TimeSystem) {
  activeId.value = system?.id || ''
  name.value = system?.name || ''
  epochLabel.value = system?.epoch_label || ''
  unitName.value = system?.unit_name || 'tick'
  displayFormat.value = system?.display_format || `{value} ${unitName.value}`
  unitsJson.value = JSON.stringify(system?.units || [], null, 2)
  calendarRulesJson.value = JSON.stringify(system?.calendar_rules || {}, null, 2)
  editing.value = Boolean(system)
}
function resetConversionForm() {
  editingConversionId.value = ''
  sourceSystem.value = selected.value?.id || props.systems[0]?.id || ''
  targetSystem.value = props.systems.find(item => item.id !== sourceSystem.value)?.id || ''
  numerator.value = 1
  denominator.value = 1
  offset.value = 0
  conversionLabel.value = ''
}
function openEditor(system = selected.value) {
  resetSystemForm(system)
  expanded.value = true
}
function openNewSystem() {
  resetSystemForm()
  expanded.value = true
}
async function loadConversions() {
  try {
    conversions.value = await api<Conversion[]>(`time-system-conversions/?workspace=${encodeURIComponent(props.workspace)}`)
  } catch (error) { emit('error', String(error)) }
}
async function saveSystem() {
  if (!name.value.trim()) return
  let units: unknown
  let calendarRules: unknown
  try { units = JSON.parse(unitsJson.value || '[]') } catch { emit('error', '时间单位 JSON 无法解析'); return }
  try { calendarRules = JSON.parse(calendarRulesJson.value || '{}') } catch { emit('error', '日历规则 JSON 无法解析'); return }
  if (!Array.isArray(units)) { emit('error', '时间单位必须是数组'); return }
  if (!calendarRules || typeof calendarRules !== 'object' || Array.isArray(calendarRules)) { emit('error', '日历规则必须是对象'); return }
  saving.value = true
  try {
    const payload = {
      workspace: props.workspace, name: name.value.trim(), epoch_label: epochLabel.value,
      unit_name: unitName.value.trim() || 'tick', display_format: displayFormat.value || '{value} {unit}', units,
      calendar_rules: calendarRules,
    }
    const result = activeId.value
      ? await api<TimeSystem>(`time-systems/${activeId.value}/`, 'PATCH', payload)
      : await api<TimeSystem>('time-systems/', 'POST', payload)
    emit('select', result.id)
    emit('updated')
    resetSystemForm(result)
  } catch (error) { emit('error', String(error)) } finally { saving.value = false }
}
async function deleteSystem(system: TimeSystem) {
  if (!confirm(`归档时间体系“${system.name}”？其时间事实不会自动删除。`)) return
  try {
    await api(`time-systems/${system.id}/`, 'DELETE')
    if (props.selectedSystem === system.id) emit('select', props.systems.find(item => item.id !== system.id)?.id || '')
    emit('updated')
  } catch (error) { emit('error', String(error)) }
}
function editConversion(item: Conversion) {
  editingConversionId.value = item.id
  sourceSystem.value = item.source_system
  targetSystem.value = item.target_system
  numerator.value = item.numerator
  denominator.value = item.denominator
  offset.value = item.offset
  conversionLabel.value = item.label
}
async function saveConversion() {
  if (!canCreateConversion.value || numerator.value <= 0 || denominator.value <= 0) return
  saving.value = true
  try {
    const payload = {
      workspace: props.workspace, source_system: sourceSystem.value, target_system: targetSystem.value,
      numerator: Number(numerator.value), denominator: Number(denominator.value), offset: Number(offset.value), label: conversionLabel.value,
    }
    if (editingConversionId.value) await api(`time-system-conversions/${editingConversionId.value}/`, 'PATCH', payload)
    else await api('time-system-conversions/', 'POST', payload)
    resetConversionForm(); await loadConversions()
  } catch (error) { emit('error', String(error)) } finally { saving.value = false }
}
async function deleteConversion(item: Conversion) {
  if (!confirm(`删除“${item.source_system_name} → ${item.target_system_name}”的换算规则？`)) return
  try { await api(`time-system-conversions/${item.id}/`, 'DELETE'); if (editingConversionId.value === item.id) resetConversionForm(); await loadConversions() }
  catch (error) { emit('error', String(error)) }
}
watch(() => props.selectedSystem, value => { if (!editing.value) resetSystemForm(props.systems.find(item => item.id === value)); void loadConversions() }, { immediate: true })
watch(() => props.systems, value => { if (!editing.value) resetSystemForm(value.find(item => item.id === props.selectedSystem)); if (!sourceSystem.value) resetConversionForm() }, { deep: true })
</script>

<template>
  <section class="time-system-editor" aria-label="时间体系编辑器">
    <div class="time-system-editor-header">
      <div><strong>时间体系与换算</strong><span class="muted">事实仍使用规范化整数；换算只负责解释与显示。</span></div>
      <div class="form-actions"><button type="button" @click="expanded = !expanded">{{ expanded ? '收起编辑器' : '编辑时间体系' }}</button><button type="button" @click="openNewSystem">＋ 新体系</button></div>
    </div>
    <div v-if="expanded" class="time-system-editor-body">
      <form class="time-system-form" @submit.prevent="saveSystem">
        <h3>{{ editing ? '编辑当前时间体系' : '创建时间体系' }}</h3>
        <label>名称<input v-model="name" required placeholder="例如：王历" /></label>
        <label>纪元标签<input v-model="epochLabel" placeholder="例如：王国建立年" /></label>
        <label>最小单位<input v-model="unitName" required placeholder="例如：日" /></label>
        <label>显示格式<input v-model="displayFormat" placeholder="{value} {unit}" /></label>
        <label>显示单位 JSON <textarea v-model="unitsJson" rows="4" spellcheck="false" placeholder='[{"name":"年","ticks":360}]'></textarea></label>
        <label>复杂日历规则 JSON <textarea v-model="calendarRulesJson" rows="7" spellcheck="false" placeholder='{"mode":"variable_months","months":[{"name":"霜月","days":28},{"name":"融月","days":35}],"leap_rule":{"cycle":5,"extra_days":2},"year_zero":false,"era":"王国纪年"}'></textarea></label>
        <div class="form-actions"><button class="primary" :disabled="saving || !name.trim()">保存时间体系</button><button v-if="editing && selected" type="button" @click="deleteSystem(selected)">删除</button></div>
      </form>
      <form class="time-system-form" @submit.prevent="saveConversion">
        <h3>{{ editingConversionId ? '编辑换算规则' : '新增有向换算' }}</h3>
        <label>来源<select v-model="sourceSystem" required><option v-for="system in systems" :key="system.id" :value="system.id">{{ system.name }}</option></select></label>
        <label>目标<select v-model="targetSystem" required><option v-for="system in systems.filter(item => item.id !== sourceSystem)" :key="system.id" :value="system.id">{{ system.name }}</option></select></label>
        <div class="conversion-equation"><label>倍率分子<input v-model.number="numerator" type="number" min="1" required /></label><span>/</span><label>倍率分母<input v-model.number="denominator" type="number" min="1" required /></label><span>＋</span><label>偏移<input v-model.number="offset" type="number" required /></label></div>
        <label>说明<input v-model="conversionLabel" placeholder="target = source × numerator / denominator + offset" /></label>
        <div class="form-actions"><button class="primary" :disabled="saving || !canCreateConversion">保存换算</button><button v-if="editingConversionId" type="button" @click="resetConversionForm">取消</button></div>
      </form>
      <div class="conversion-list"><h3>已声明换算</h3><p v-if="!conversions.length" class="muted">还没有跨时间体系的换算规则。</p><div v-for="item in conversions" :key="item.id" class="conversion-row"><div><strong>{{ item.source_system_name }} → {{ item.target_system_name }}</strong><small>{{ item.target_system_name }} = {{ item.source_system_name }} × {{ item.numerator }}/{{ item.denominator }} {{ item.offset >= 0 ? '+' : '−' }} {{ Math.abs(item.offset) }}<span v-if="item.label"> · {{ item.label }}</span></small></div><span class="event-actions"><button type="button" @click="editConversion(item)">编辑</button><button type="button" @click="deleteConversion(item)">删除</button></span></div></div>
    </div>
  </section>
</template>
