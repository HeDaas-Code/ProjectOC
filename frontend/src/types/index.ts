export const entityTypes = { canonical_setting: '设定系', character: '人物', timeline: '时间线', event: '事件', item: '物品', location: '地点', faction: '势力', floating_tip: '游离设定' } as const
export type EntityType = keyof typeof entityTypes
export interface Workspace {
  id: string
  name: string
  description: string
  settings?: { default_branch?: string; ai?: { provider?: string; model?: string; base_url?: string }; [key: string]: unknown }
  ai_api_key_configured?: boolean
  ai_api_key_last4?: string
}
export interface Membership { id: number; workspace: string; user: number | string; username: string; email: string; role: 'owner' | 'editor' | 'reader'; created_at: string }
export interface Invite { id: string; workspace: string; workspace_name?: string; email: string; role: 'editor' | 'reader'; expires_at: string; accepted_at: string | null; created_at: string; invite_url?: string }
export interface Branch { id: string; workspace: string; name: string; status: "active" | "merged" | "archived"; base_commit?: string; git_ref?: string }
export interface Canvas { id: string; purpose?: 'staging' | 'graph'; workspace: string; branch: string; name: string; snapshot: unknown; snapshot_version: number; status: string }
export interface CanvasContainer { id: string; workspace: string; branch: string; parent: string | null; canvas: string; canvas_name: string; name: string; sort_order: number; status: string; children: string[] }
export interface Proposal { id: string; canvas: string; entity_type: EntityType; title: string; content: string; metadata: Record<string, unknown>; status: string; created_entity: string | null; updated_at: string; conflicts: { message: string; entityId?: string }[]; suggested_relations: RelationProposal[] }
export interface RelationProposal { id: string; source_proposal: string; target_proposal: string | null; target_entity: string | null; target_title: string; relation_type: string; reason: string; status: string; time_system: string | null; valid_from: number | null; valid_to: number | null }
export interface Entity { id: string; type: EntityType; title: string; content: string; status: "active" | "archived"; git_path: string; commit_hash: string; sync_status: string }
export interface Question { text: string; reason: string; priority: string; status: string }
export interface Message { id: string; role: string; content: string }
export interface Session { id: string; model: string; messages: Message[]; context: { question?: Question; [key: string]: unknown } }
export interface DialogueMemory {
  scope_key: string
  revision: number
  summary: string
  confirmed_facts: Record<string, unknown>[]
  working_notes: Record<string, unknown>[]
  open_questions: Record<string, unknown>[]
  recent_session_summary: string
  manual_notes: { text: string; updated_at?: string }[]
  archived_questions: { text: string; reason?: string; archived_at?: string }[]
  audit?: { id: string; action: string; before: Record<string, unknown>; after: Record<string, unknown>; actor: string | null; created_at: string }[]
  updated_at: string
}
export interface Job { id: string; status: string; commit_hash: string; error_message: string }

export interface AgentEvidence {
  id?: string
  source_type: string
  source_id: string
  field_path: string
  source_revision: string
  excerpt: string
  relevance: number
}
export interface AgentFinding {
  severity: 'info' | 'warning' | 'error'
  category: 'temporal' | 'identity' | 'relation' | 'branch' | 'missing_context' | string
  title: string
  explanation: string
  evidence: AgentEvidence[]
  suggested_questions: string[]
  confidence: number
  suggested_resolution?: Record<string, unknown>
}
export interface AgentResult {
  branch: string
  mode: string
  findings: AgentFinding[]
  evidence: AgentEvidence[]
  summary: { total: number; errors: number; warnings: number }
}
export interface AgentRun {
  id: string
  workspace: string
  branch: string
  mode: string
  status: string
  model: string
  context_revision: number
  output?: AgentResult
  validation_errors: string[]
  token_budget: number
  input_tokens: number
  output_tokens: number
  latency_ms: number
  fallback_used: boolean
  created_at: string
  completed_at?: string | null
}
export interface SnapshotRef {
  kind?: 'commit' | 'snapshot'
  id?: string
  commit?: string
  snapshot?: string
  label?: string
  revision?: string | number
}
export interface TimelineEventDiff {
  id: string
  timeline_id?: string
  event_id?: string
  title?: string
  start_value?: number | null
  end_value?: number | null
  before?: Record<string, unknown>
  after?: Record<string, unknown>
  change_kind?: 'added' | 'removed' | 'moved' | 'modified' | string
  changed_fields?: { field: string; before: unknown; after: unknown }[]
  evidence?: { source_type: string; source_id: string; source_revision?: string; field_path?: string }[]
}
export interface LifecycleDiff {
  id?: string
  character?: string
  character_title?: string
  before?: Record<string, unknown>
  after?: Record<string, unknown>
  change_kind?: 'added' | 'removed' | 'changed' | string
  changed_fields?: { field: string; before: unknown; after: unknown }[]
}
export interface ParticipantDiff {
  entry_id: string
  entry_title?: string
  before: unknown[]
  after: unknown[]
  added?: unknown[]
  removed?: unknown[]
}
export interface RelationDiff {
  id?: string
  source?: string
  target?: string
  relation_type?: string
  before?: Record<string, unknown>
  after?: Record<string, unknown>
  change_kind?: 'added' | 'removed' | 'changed' | string
}
export interface TimelineWarning {
  code?: string
  severity?: 'info' | 'warning' | 'error' | string
  message: string
  event_id?: string
  entity_id?: string
  evidence?: { source_type: string; source_id: string; source_revision?: string; field_path?: string }[]
}
export interface TimelineDiff {
  from: string | SnapshotRef
  to: string | SnapshotRef
  from_ref?: SnapshotRef
  to_ref?: SnapshotRef
  schema_compatible?: boolean
  schema_version?: string
  addedEvents: TimelineEventDiff[]
  removedEvents: TimelineEventDiff[]
  changedEvents: TimelineEventDiff[]
  lifecycleChanges: { added: LifecycleDiff[]; removed: LifecycleDiff[]; changed: LifecycleDiff[] }
  participantChanges: ParticipantDiff[]
  relationChanges: { added: RelationDiff[]; removed: RelationDiff[]; changed: RelationDiff[] }
  timeSystemChanges: Record<string, unknown> | { added?: unknown[]; removed?: unknown[]; changed?: unknown[] }
  warnings: (string | TimelineWarning)[]
}
