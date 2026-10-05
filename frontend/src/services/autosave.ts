/** Serializes saves; an edit made during a request is never marked as already saved. */
export class SaveQueue {
  private timer?: ReturnType<typeof setTimeout>
  private latest: unknown
  private dirty = false
  private active?: Promise<void>
  private paused = false
  stopped = false
  constructor(private save: (value: unknown) => Promise<void>, private failed: (error: unknown) => void, private delay = 700) {}
  enqueue(value: unknown) {
    this.latest = value
    this.dirty = true
    clearTimeout(this.timer)
    if (!this.stopped && !this.paused) this.timer = setTimeout(() => void this.flush().catch(() => {}), this.delay)
  }
  /**
   * Pause the snapshot fallback while a collaboration transport is being
   * established. Record operations are the authoritative path in that mode;
   * allowing an old full snapshot to race them can overwrite a newer canvas.
   */
  pause() { this.paused = true; clearTimeout(this.timer) }
  resume() {
    this.paused = false
    if (this.dirty && !this.stopped) {
      clearTimeout(this.timer)
      this.timer = setTimeout(() => void this.flush().catch(() => {}), this.delay)
    }
  }
  async flush(): Promise<void> {
    clearTimeout(this.timer)
    if (this.active) { await this.active; if (this.dirty) return this.flush(); return }
    if (!this.dirty || this.paused) return
    if (this.stopped) throw new Error('有待同步编辑，请重试或导出本地副本')
    const snapshot = this.latest; this.dirty = false
    this.active = this.save(snapshot).catch(e => { this.dirty = true; this.stopped = true; this.failed(e); throw e }).finally(() => { this.active = undefined })
    await this.active
    if (this.dirty && !this.paused) await this.flush()
  }
  clearPending() { this.dirty = false; this.latest = undefined; clearTimeout(this.timer) }
  async retry() { this.stopped = false; await this.flush() }
  dispose() { clearTimeout(this.timer) }
}
