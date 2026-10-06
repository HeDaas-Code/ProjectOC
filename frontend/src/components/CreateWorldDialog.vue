<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps<{ open: boolean; busy: boolean; error: string; nameLocked: boolean }>()
const emit = defineEmits<{ close: []; submit: [name: string] }>()
const dialog = ref<HTMLDialogElement>()
const nameInput = ref<HTMLInputElement>()
const name = ref('')
const validName = computed(() => name.value.trim())

watch(() => props.open, async open => {
  await nextTick()
  if (open && props.open) {
    name.value = ''
    dialog.value?.showModal()
    await nextTick()
    nameInput.value?.focus()
  } else if (!props.open) dialog.value?.close()
}, { immediate: true })
function close() { if (!props.busy) emit('close') }
function backdropClick(event: MouseEvent) {
  if (event.target !== dialog.value) return
  const bounds = dialog.value.getBoundingClientRect()
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close()
}
function submit() { if (validName.value && !props.busy) emit('submit', validName.value) }
onBeforeUnmount(() => dialog.value?.close())
</script>

<template>
  <dialog ref="dialog" class="create-world-dialog" aria-labelledby="create-world-title" aria-describedby="create-world-description" @cancel.prevent="close" @click="backdropClick">
    <form class="create-world-form" :aria-busy="busy" @submit.prevent="submit">
      <button type="button" class="dialog-close" aria-label="关闭创建世界观弹窗" :disabled="busy" @click="close">×</button>
      <div class="world-symbol" aria-hidden="true">◇</div>
      <p class="eyebrow">EVERY WORLD BEGINS WITH A QUESTION</p>
      <h2 id="create-world-title">为你的世界，写下第一个名字</h2>
      <p id="create-world-description" class="world-description">一个名字，一页空白。让散落的灵感从这里生长。</p>
      <label for="new-world-name">世界观名称</label>
      <input id="new-world-name" ref="nameInput" v-model="name" placeholder="例如：群星之外、雾海纪事…" maxlength="200" required autocomplete="off" :disabled="busy || nameLocked" aria-describedby="world-name-hint" />
      <div id="world-name-hint" class="world-name-hint"><span>为你准备一张新的灵感画布。</span><span>{{ name.length }} / 200</span></div>
      <p v-if="error" class="world-create-error" role="alert">{{ error }}</p>
      <footer class="world-dialog-actions">
        <button type="button" :disabled="busy" @click="close">暂时取消</button>
        <button type="submit" class="primary" :disabled="busy || !validName">{{ busy ? '正在准备你的世界…' : nameLocked ? '继续准备画布 →' : '创建世界观 →' }}</button>
      </footer>
    </form>
  </dialog>
</template>

<style scoped>
.create-world-dialog{width:min(480px,calc(100vw - 32px));max-height:calc(100dvh - 40px);padding:0;border:1px solid var(--line);border-radius:18px;color:inherit;background:#fafbf7;box-shadow:0 24px 80px rgba(32,53,40,.18);overflow:auto}
.create-world-dialog::backdrop{background:rgba(34,49,39,.3);backdrop-filter:blur(4px)}
.create-world-form{position:relative;padding:36px}
.dialog-close{position:absolute;top:14px;right:14px;width:30px;height:30px;padding:0;border:0;background:transparent;color:var(--muted);font-size:23px}
.world-symbol{display:grid;place-items:center;width:46px;height:46px;margin-bottom:22px;border:1px solid #d9e4d4;border-radius:12px;background:#eef3e9;color:var(--green);font-size:32px}
.eyebrow{font-size:9px;letter-spacing:1.4px;line-height:1.6;margin-bottom:10px}
h2{margin-bottom:10px;color:#344b3c;font-size:24px;line-height:1.55;font-weight:400}
.world-description{margin-bottom:28px;color:var(--muted);font-size:12px;line-height:1.8}
label{display:block;margin-bottom:9px;font-size:12px;color:#526754}
input{width:100%;min-height:46px;padding:12px 14px;border-radius:9px;background:#fff;font-size:14px}
.world-name-hint{display:flex;justify-content:space-between;gap:12px;margin-top:9px;color:var(--muted);font-size:10px;line-height:1.6}
.world-create-error{margin:16px 0 0;padding:10px 12px;border-radius:8px;background:#faf0ea;color:#9a5546;font-size:12px;line-height:1.6}
.world-dialog-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:28px;padding-top:20px;border-top:1px solid var(--line)}
.world-dialog-actions button{min-height:38px;padding:9px 14px}
button:focus-visible{outline:2px solid #94ad8c;outline-offset:3px}
@media(max-width:480px){.create-world-form{padding:28px 22px}h2{font-size:21px}.world-dialog-actions{flex-wrap:wrap}.world-dialog-actions button{flex:1}}
</style>
