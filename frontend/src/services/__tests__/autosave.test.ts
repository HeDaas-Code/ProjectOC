import { afterEach, describe, expect, it, vi } from 'vitest'
import { SaveQueue } from '../autosave'

describe('SaveQueue collaboration fallback', () => {
  afterEach(() => vi.useRealTimers())

  it('does not write a snapshot while paused, then resumes the latest snapshot', async () => {
    vi.useFakeTimers()
    const save = vi.fn(async () => {})
    const failed = vi.fn()
    const queue = new SaveQueue(save, failed, 20)

    queue.pause()
    queue.enqueue({ revision: 1 })
    await vi.advanceTimersByTimeAsync(100)
    expect(save).not.toHaveBeenCalled()

    queue.resume()
    await vi.advanceTimersByTimeAsync(20)
    await queue.flush()
    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledWith({ revision: 1 })
    expect(failed).not.toHaveBeenCalled()
  })

  it('clears a stale REST fallback after the realtime path owns the edit', async () => {
    vi.useFakeTimers()
    const save = vi.fn(async () => {})
    const queue = new SaveQueue(save, vi.fn(), 20)

    queue.enqueue({ revision: 1 })
    queue.clearPending()
    await vi.advanceTimersByTimeAsync(100)
    expect(save).not.toHaveBeenCalled()
  })
})
