import api from './client'

export interface PermissionTemplateRevision {
  id: string
  revision: number
  privileges: string[]
  grant_option: boolean
  created_at: string
}

export interface PermissionTemplate {
  id: string
  name: string
  description: string | null
  revisions: PermissionTemplateRevision[]
  created_at: string
}

export interface PermissionSyncTarget {
  id: string
  member_id: string
  resource_id: string | null
  previous_revision_id: string | null
  status: string
  change_required: boolean
  retry_count: number
  failure_reason: string | null
}

export interface PermissionSyncJob {
  id: string
  template_revision_id: string
  target_scope: 'pool' | 'resource'
  target_id: string
  mode: 'dry_run' | 'apply'
  status: string
  total_count: number
  completed_count: number
  failed_count: number
  failure_reason: string | null
  targets: PermissionSyncTarget[]
}

export const listPermissionTemplates = () =>
  api.get<PermissionTemplate[]>('/api/permission-templates')

export const createPermissionTemplateRevision = (
  templateId: string,
  input: { privileges: string[]; grant_option: boolean },
) =>
  api.post<PermissionTemplateRevision>(
    `/api/permission-templates/${encodeURIComponent(templateId)}/revisions`,
    input,
  )

export const requestPermissionSync = (
  revisionId: string,
  input: {
    target_scope: 'pool' | 'resource'
    target_id: string
    mode: 'dry_run' | 'apply'
    confirmed: boolean
  },
) =>
  api.post<PermissionSyncJob>(
    `/api/permission-template-revisions/${encodeURIComponent(revisionId)}/sync`,
    input,
  )

export const getPermissionSyncJob = (jobId: string) =>
  api.get<PermissionSyncJob>(
    `/api/permission-sync-jobs/${encodeURIComponent(jobId)}`,
  )
