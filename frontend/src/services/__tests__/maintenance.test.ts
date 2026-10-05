import { describe, expect, it } from 'vitest'
import { gitHealthLabel, maintenanceActionLabel, maintenanceModeLabel, maintenancePriorityLabel, pendingJobCount, projectionHealthLabel } from '../maintenance'

describe('maintenance status helpers', () => {
  it('counts pending jobs across statuses', () => {
    expect(pendingJobCount({ database_committed: 2, git_sync_failed: 1 })).toBe(3)
    expect(pendingJobCount(undefined)).toBe(0)
  })

  it('prioritizes a missing main ref and orphan refs in Git status', () => {
    expect(gitHealthLabel({ main_ref_exists: false, orphan_refs: [] })).toBe('main 缺失')
    expect(gitHealthLabel({ main_ref_exists: true, orphan_refs: ['oc/branch/lost'] })).toBe('1 个孤立 ref')
    expect(gitHealthLabel({ main_ref_exists: true, orphan_refs: [] })).toBe('Git 正常')
  })

  it('explains optional projection fallback and retryable failures', () => {
    expect(projectionHealthLabel({ available: false, configured: false, last_job: null })).toContain('回退 PostgreSQL')
    expect(projectionHealthLabel({ available: false, configured: true, last_job: { id: '1', status: 'failed', attempts: 2, error: 'timeout' } })).toBe('投影失败，可重试')
    expect(projectionHealthLabel({ available: true, configured: true, last_job: null })).toBe('Neo4j 可用')
  })
})


describe('maintenance review helpers', () => {
  it('labels advisory actions and priorities without treating them as facts', () => {
    expect(maintenanceActionLabel('draft_proposal')).toBe('可形成草稿提案')
    expect(maintenancePriorityLabel('conflict')).toBe('设定冲突')
    expect(maintenanceModeLabel('deterministic_fallback')).toBe('离线规则审阅')
  })
})
