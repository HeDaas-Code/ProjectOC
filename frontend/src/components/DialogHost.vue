<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import StyledDialog from './StyledDialog.vue'
import { activeDialog, finishDialog } from '../services/dialog'
const values = ref<Record<string, string>>({})
watch(activeDialog, request => {
  values.value = Object.fromEntries((request?.fields || []).map(field => [field.key, field.value ?? field.options?.[0]?.value ?? '']))
})
const valid = computed(() => (activeDialog.value?.fields || []).every(field => {
  const value = values.value[field.key]?.trim()
  return value && (!field.options || field.options.some(option => option.value === value))
}))
function submit() { if (valid.value) finishDialog(Object.fromEntries(Object.entries(values.value).map(([key, value]) => [key, value.trim()]))) }
</script>
<template>
  <StyledDialog v-if="activeDialog" :key="activeDialog.title" :open="true" :title="activeDialog.title" :description="activeDialog.description" @close="finishDialog(null)">
    <form novalidate @submit.prevent="submit">
      <label v-for="field in activeDialog.fields" :key="field.key">{{ field.label }}
        <select v-if="field.options" v-model="values[field.key]" :aria-label="field.label"><option v-for="option in field.options" :key="option.value" :value="option.value">{{ option.label }}</option></select>
        <input v-else v-model="values[field.key]" :aria-label="field.label" :placeholder="field.placeholder" maxlength="200" autocomplete="off" />
      </label>
      <div class="actions"><button type="button" @click="finishDialog(null)">取消</button><button type="submit" :class="activeDialog.danger ? 'dialog-danger' : 'primary'" :disabled="!valid">{{ activeDialog.confirmLabel || '确认' }}</button></div>
    </form>
  </StyledDialog>
</template>
