import { logError, logInfo, previewText, nextTraceId } from './logger'

const API = '/api'

export interface Message {
  role: 'user' | 'assistant'
  content: string
  timestamp?: string
}

export interface SystemStatus {
  initialized: boolean
  subsystems: Record<string, string>
  working_memory_size: number
  default_local_model: string
  frontier_model: string
  telegram_enabled: boolean
  scheduler?: {
    running: boolean
    jobs: string[]
    last_brief: string | null
    last_review: string | null
  }
}

export interface Commitment {
  id: number
  content: string
  importance_score: number
  created_at: string
}

// Epic 01 (#24) — trace inspection UI types.
export interface TraceSummary {
  trace_id: string
  created_at: string
  trigger_source: string
  archetype: string
  model_selected: string
  latency_ms: number
  data_sensitivity: string
  tier: string
  scheduler_job_name: string | null
  // #34 — projected from assembled_context so the inspector can find the
  // previous trace with a different capability block version without
  // issuing one detail fetch per summary.
  capability_block_version?: string | null
}

export interface TraceDetail extends TraceSummary {
  input: string
  output: string
  model_version: string
  prompt_version: string
  assembled_context: Record<string, unknown>
  tools_called: Array<Record<string, unknown>>
  user_reaction: Record<string, unknown> | null
  embedding_model_version: string | null
  has_embedding: boolean
  // #34 — selector → human reason map computed off stored provenance.
  decision_reasons: Record<string, string>
}

// Epic 01 (#34) — context inspector re-render endpoint.
export interface RerenderPromptResponse {
  trace_id: string
  prompt: string
  prompt_hash: string
  provenance: Record<string, unknown>
  original_provenance: Record<string, unknown>
  matches_original: boolean
  notes: string[]
}

// Epic 01 (#34) — single-memory fetch for inspector expandable rows.
export interface MemoryDetail {
  id: number
  type: string
  content: string
  summary: string | null
  importance_score: number
  created_at: string
  accessed_at: string | null
  has_embedding: boolean
}

export interface TraceListResponse {
  traces: TraceSummary[]
  next_cursor: string | null
}

function getBodyPreview(body: RequestInit['body']) {
  if (typeof body !== 'string') {
    return undefined
  }

  return previewText(body, 220)
}

async function req<T>(path: string, options?: RequestInit): Promise<T> {
  const requestId = nextTraceId('api')
  const method = options?.method ?? 'GET'
  const startedAt = performance.now()

  logInfo('api', 'request_started', {
    requestId,
    method,
    path,
    bodyPreview: getBodyPreview(options?.body),
  })

  let res: Response

  try {
    res = await fetch(`${API}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    })
  } catch (error) {
    logError('api', 'request_network_failed', {
      requestId,
      method,
      path,
      durationMs: Math.round(performance.now() - startedAt),
      error,
    })
    throw error
  }

  const rawText = await res.text()
  const durationMs = Math.round(performance.now() - startedAt)

  if (!res.ok) {
    logError('api', 'request_failed', {
      requestId,
      method,
      path,
      status: res.status,
      statusText: res.statusText,
      durationMs,
      responsePreview: previewText(rawText, 220),
    })
    throw new Error(`${res.status} ${res.statusText}`)
  }

  logInfo('api', 'request_succeeded', {
    requestId,
    method,
    path,
    status: res.status,
    durationMs,
    responsePreview: previewText(rawText, 220),
  })

  try {
    return JSON.parse(rawText) as T
  } catch (error) {
    logError('api', 'response_parse_failed', {
      requestId,
      method,
      path,
      durationMs,
      responsePreview: previewText(rawText, 220),
      error,
    })
    throw error
  }
}

export const api = {
  chat: (message: string, sessionId: string) =>
    req<{ response: string; session_id: string }>('/chat', {
      method: 'POST',
      body: JSON.stringify({ message, session_id: sessionId }),
    }),

  getStatus: () => req<SystemStatus>('/status'),

  getLifeContext: () => req<{ content: string; path: string }>('/life-context'),

  getConversations: (limit = 50) =>
    req<Array<{ id: number; session_id: string; role: string; content: string; created_at: string }>>(
      `/conversations?limit=${limit}`
    ),

  triggerBrief: () => req<{ ok: boolean; brief: string }>('/brief/now', { method: 'POST' }),

  triggerReview: () => req<{ ok: boolean; review: string }>('/review/now', { method: 'POST' }),

  getCommitments: () => req<{ commitments: Commitment[] }>('/commitments'),

  completeCommitment: (id: number) =>
    req<{ ok: boolean }>(`/commitments/${id}/complete`, { method: 'POST' }),

  health: () => req<{ status: string }>('/health'),

  getCapabilities: () =>
    req<{
      capabilities: Record<string, {
        display_name: string
        status: 'available' | 'not_configured' | 'permission_required' | 'temporarily_unavailable' | 'disabled'
        detail: string
        accounts: string[]
      }>
      available: string[]
    }>('/capabilities'),

  refreshCapabilities: () =>
    req<{ ok: boolean; capabilities: Record<string, unknown>; available: string[] }>(
      '/capabilities/refresh', { method: 'POST' }
    ),

  getPendingActions: () =>
    req<{
      pending: Array<{
        id: string
        tool_name: string
        args: Record<string, unknown>
        preview: string
        model_description?: string
        created_at: string
      }>
      count: number
    }>('/pending-actions'),

  actOnPending: (id: string, action: 'approve' | 'reject' | 'edit', edited_body?: string) =>
    req<{ ok: boolean }>(`/pending-actions/${id}`, {
      method: 'POST',
      body: JSON.stringify({ action, edited_body }),
    }),

  getCommsHealth: (quietDays = 14) =>
    req<{
      summary: {
        signals: string[]
        quiet_contact_count: number
        overdue_response_count: number
        summary: string
      }
      overdue_responses: {
        overdue: Array<{ from: string; channel: string; unread_count: number; last_message_at: string | null }>
        count: number
        summary: string
      }
      relationship_balance: {
        personal_contacts: number
        work_contacts: number
        personal_pct: number
        work_pct: number
        balance_note: string
        summary: string
      }
    }>(`/comms-health?quiet_days=${quietDays}`),

  // Epic 01 (#24) — trace inspection.
  listTraces: (params: {
    limit?: number
    cursor?: string
    archetype?: string
    triggerSource?: string
    modelSelected?: string
    dataSensitivity?: string
    tier?: string
    containsText?: string
  } = {}) => {
    const q = new URLSearchParams()
    if (params.limit) q.set('limit', String(params.limit))
    if (params.cursor) q.set('cursor', params.cursor)
    if (params.archetype) q.set('archetype', params.archetype)
    if (params.triggerSource) q.set('trigger_source', params.triggerSource)
    if (params.modelSelected) q.set('model_selected', params.modelSelected)
    if (params.dataSensitivity) q.set('data_sensitivity', params.dataSensitivity)
    if (params.tier) q.set('tier', params.tier)
    if (params.containsText) q.set('contains_text', params.containsText)
    const qs = q.toString()
    return req<TraceListResponse>(`/traces${qs ? `?${qs}` : ''}`)
  },

  getTrace: (traceId: string) => req<TraceDetail>(`/traces/${traceId}`),

  // Epic 01 (#34) — re-render the prompt for a trace using the live
  // assembler. Result is in-browser only — never logged server-side.
  rerenderPrompt: (traceId: string) =>
    req<RerenderPromptResponse>(`/traces/${traceId}/rerender-prompt`, {
      method: 'POST',
    }),

  // Epic 01 (#34) — fetch a single memory's content + metadata. Used by
  // the trace inspector to expand a memory row from its provenance ID.
  getMemory: (memoryId: string | number) =>
    req<MemoryDetail>(`/memories/${memoryId}`),

  // Epic 04 (#41) — pattern detector alerts panel.
  listReflectorAlerts: (status: 'open' | 'dismissed' | 'filed' | 'all' = 'open') =>
    req<{ alerts: ReflectorAlert[] }>(`/reflector/alerts?status=${status}`),

  dismissReflectorAlert: (alertId: string) =>
    req<{ ok: boolean; alert_id: string; status: string }>(
      `/reflector/alerts/${alertId}/dismiss`,
      { method: 'POST' },
    ),

  fileReflectorAlert: (alertId: string) =>
    req<{ ok: boolean; alert_id: string; status: string }>(
      `/reflector/alerts/${alertId}/file`,
      { method: 'POST' },
    ),
}

// Epic 04 (#41) — pattern detector alerts.
export interface ReflectorAlert {
  alert_id: string
  created_at: string
  window_start: string
  window_end: string
  trace_ids: string[]
  cluster_size: number
  confidence: number
  summary: string
  suggested_action: string
  status: 'open' | 'dismissed' | 'filed'
  metadata: Record<string, unknown>
}
