<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
const props = defineProps<{ open: boolean; title: string; description?: string; busy?: boolean; wide?: boolean }>()
const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDialogElement>()
function close() { if (!props.busy) emit('close') }
watch(() => props.open, async open => {
  await nextTick()
  if (open && props.open && !dialog.value?.open) {
    dialog.value?.showModal()
    dialog.value?.querySelector<HTMLElement>('input, select, textarea, .actions button')?.focus()
  }
  else if (!props.open) dialog.value?.close()
}, { immediate: true })
function backdrop(event: MouseEvent) {
  if (event.target !== dialog.value) return
  const rect = dialog.value.getBoundingClientRect()
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close()
}
onBeforeUnmount(() => dialog.value?.close())
</script>
<template>
  <dialog ref="dialog" class="styled-dialog" :class="{ wide }" :aria-label="title" :aria-busy="busy" @cancel.prevent="close" @click="backdrop">
    <div class="styled-dialog-content">
      <button type="button" class="styled-dialog-close" aria-label="关闭弹窗" :disabled="busy" @click="close">×</button>
      <span class="eyebrow">PROJECT OC · 世界观工作台</span>
      <h2>{{ title }}</h2><p v-if="description" class="styled-dialog-description">{{ description }}</p>
      <slot />
    </div>
  </dialog>
</template>
<style>
.styled-dialog{width:min(480px,calc(100vw - 32px));max-height:calc(100dvh - 40px);padding:0;border:1px solid var(--line);border-radius:18px;color:inherit;background:#fafbf7;box-shadow:0 24px 80px #2035282e;overflow:auto}
.styled-dialog.wide{width:min(800px,calc(100vw - 32px))}.styled-dialog::backdrop{background:#2231274d;backdrop-filter:blur(4px)}
.styled-dialog-content{position:relative;padding:32px}.styled-dialog-close{position:absolute;top:12px;right:12px;width:30px;height:30px;padding:0;border:0;background:transparent;color:var(--muted);font-size:23px}
.styled-dialog h2{margin:12px 0 16px;color:#344b3c;font-size:23px;font-weight:400}.styled-dialog-description{font-size:13px;line-height:1.8;color:var(--muted);white-space:pre-wrap;margin-bottom:22px}
.styled-dialog label{display:block;margin:18px 0 8px;color:#526754;font-size:12px}.styled-dialog input,.styled-dialog select,.styled-dialog textarea{width:100%;margin-top:8px;padding:12px;border-radius:9px;background:#fff}.styled-dialog pre{max-height:50vh;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}
.styled-dialog .actions{display:flex;justify-content:flex-end;gap:10px;margin-top:26px;padding-top:18px;border-top:1px solid var(--line)}.styled-dialog button:focus-visible{outline:2px solid #94ad8c;outline-offset:3px}.dialog-danger{background:#97584c;color:white;border-color:#97584c}
@media(max-width:480px){.styled-dialog-content{padding:28px 22px}.styled-dialog .actions{flex-wrap:wrap}}
</style>
