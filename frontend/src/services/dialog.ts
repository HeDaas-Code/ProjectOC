import { shallowRef } from 'vue'

export type DialogField = { key: string; label: string; value?: string; placeholder?: string; options?: { value: string; label: string }[] }
export type DialogOptions = { title: string; description?: string; confirmLabel?: string; danger?: boolean; fields?: DialogField[] }
type Request = DialogOptions & { resolve: (value: Record<string, string> | null) => void }
export const activeDialog = shallowRef<Request>()
const queue: Request[] = []
export function requestDialog(options: DialogOptions): Promise<Record<string, string> | null> {
  return new Promise(resolve => { queue.push({ ...options, resolve }); if (!activeDialog.value) activeDialog.value = queue.shift() })
}
export function finishDialog(value: Record<string, string> | null) {
  const current = activeDialog.value
  activeDialog.value = queue.shift()
  current?.resolve(value)
}
export async function confirmAction(description: string, title = '确认操作', confirmLabel = '确认') {
  return Boolean(await requestDialog({ title, description, confirmLabel, danger: true }))
}
