export interface GitBranchHealth {
  id: string
  name: string
  status: string
  git_ref: string
  ref_exists: boolean
  head_commit: string
  base_commit: string
  base_exists: boolean
  base_matches_head: boolean
}

export interface GitHealth {
  repository: string
  main_ref_exists: boolean
  main_commit: string
  branches: GitBranchHealth[]
  orphan_refs: string[]
  pending_jobs: Record<string, number>
}

export interface ProjectionJobHealth {
  id: string
  status: string
  attempts: number
  error: string
}

export interface ProjectionHealth {
  available: boolean
  configured: boolean
  error?: string
  last_job: ProjectionJobHealth | null
}

export function pendingJobCount(pending: Record<string, number> | undefined): number {
  return Object.values(pending || {}).reduce((total, count) => total + count, 0)
}

export function gitHealthLabel(status: Pick<GitHealth, 'main_ref_exists' | 'orphan_refs'>): string {
  if (!status.main_ref_exists) return 'main 缺失'
  if (status.orphan_refs.length) return `${status.orphan_refs.length} 个孤立 ref`
  return 'Git 正常'
}

export function projectionHealthLabel(status: Pick<ProjectionHealth, 'available' | 'configured' | 'last_job'>): string {
  if (status.available) return 'Neo4j 可用'
  if (!status.configured) return '未配置（已回退 PostgreSQL）'
  if (status.last_job?.status === 'failed') return '投影失败，可重试'
  return 'Neo4j 不可用（已回退 PostgreSQL）'
}


export interface ConsistencyIssue {
  code: string
  severity: 'error' | 'warning' | 'info'
  message: string
  entity_id?: string
  relation_id?: string
  metadata?: Record<string, unknown>
}

export interface ConsistencyReport {
  branch: string
  score: number
  summary: { error: number; warning: number; info: number }
  issues: ConsistencyIssue[]
  entity_count: number
  relation_count: number
}

export function consistencyHealthLabel(report: ConsistencyReport): string {
  if (report.summary.error) return `${report.summary.error} 个错误`
  if (report.summary.warning) return `${report.summary.warning} 个提醒`
  return '一致性良好'
}


export type MaintenanceAction = 'ask' | 'inspect' | 'draft_proposal'
export type MaintenancePriority = 'conflict' | 'logic' | 'connection' | 'attribute' | 'expansion'

export interface MaintenanceSuggestion {
  issue_code: string
  action: MaintenanceAction
  title: string
  reason: string
  target_ids: string[]
}

export interface MaintenanceQuestion {
  text: string
  reason: string
  priority: MaintenancePriority
}

export interface MaintenanceReview {
  mode: 'upstream' | 'deterministic_fallback'
  status?: 'unavailable'
  branch: string
  report: ConsistencyReport
  suggestions: MaintenanceSuggestion[]
  question: MaintenanceQuestion
}



export interface MaintenanceProposalRequest {
  canvas: string
  branch?: string
  action: 'draft_proposal'
  issue_code: string
  target_ids?: string[]
  title: string
  content: string
  entity_type: string
  reason?: string
  idempotency_key?: string
  metadata?: Record<string, unknown>
}

export interface MaintenanceProposalResponse {
  id: string
  status: string
  title: string
  content: string
  entity_type: string
  canvas: string
  idempotent?: boolean
}

export function maintenanceActionLabel(action: MaintenanceAction): string {
  return ({
    ask: '先问一个问题',
    inspect: '建议检查',
    draft_proposal: '可形成草稿提案',
  } satisfies Record<MaintenanceAction, string>)[action]
}

export function maintenancePriorityLabel(priority: MaintenancePriority): string {
  return ({
    conflict: '设定冲突',
    logic: '基础逻辑',
    connection: '关键连接',
    attribute: '属性深化',
    expansion: '可选扩展',
  } satisfies Record<MaintenancePriority, string>)[priority]
}

export function maintenanceModeLabel(mode: MaintenanceReview['mode']): string {
  return mode === 'upstream' ? '上游 Agent 审阅' : '离线规则审阅'
}
