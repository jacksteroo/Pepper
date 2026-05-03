import { useEffect, useState } from 'react'
import { api, type TraceDetail, type TraceSummary } from '../api'
import { logError, logInfo } from '../logger'
import TraceContextInspector from './TraceContextInspector'

const styles = {
  root: { display: 'flex', height: '100%', overflow: 'hidden' as const },
  list: {
    width: '40%',
    minWidth: 320,
    borderRight: '1px solid #1e1e1e',
    display: 'flex',
    flexDirection: 'column' as const,
  },
  filters: {
    padding: '12px 16px',
    borderBottom: '1px solid #1e1e1e',
    display: 'flex',
    gap: 8,
    flexWrap: 'wrap' as const,
  },
  input: {
    background: '#0a0a0a',
    color: '#e0e0e0',
    border: '1px solid #2a2a2a',
    borderRadius: 4,
    padding: '4px 8px',
    fontSize: 12,
    fontFamily: 'inherit',
  },
  rows: { flex: 1, overflowY: 'auto' as const },
  row: (selected: boolean) => ({
    padding: '10px 16px',
    borderBottom: '1px solid #161616',
    cursor: 'pointer',
    background: selected ? '#1a2738' : 'transparent',
  }),
  rowMeta: { fontSize: 11, color: '#888', marginTop: 4 },
  pill: (color: string) => ({
    display: 'inline-block',
    padding: '1px 6px',
    borderRadius: 3,
    fontSize: 10,
    background: `${color}22`,
    color,
    marginRight: 4,
  }),
  detail: {
    flex: 1,
    overflowY: 'auto' as const,
    padding: 24,
  },
  inspectButton: {
    marginTop: 12,
    background: '#1e3a5f',
    color: '#bfdbfe',
    border: '1px solid #2a4a73',
    borderRadius: 4,
    padding: '6px 12px',
    cursor: 'pointer',
    fontSize: 12,
    fontFamily: 'inherit',
  },
  header: { fontSize: 18, fontWeight: 600, marginBottom: 4 },
  field: { marginTop: 16 },
  label: { fontSize: 11, color: '#888', textTransform: 'uppercase' as const, letterSpacing: 0.5 },
  value: {
    marginTop: 4,
    background: '#0a0a0a',
    padding: 12,
    borderRadius: 4,
    border: '1px solid #1e1e1e',
    fontFamily: 'ui-monospace, monospace',
    fontSize: 12,
    whiteSpace: 'pre-wrap' as const,
    overflowX: 'auto' as const,
  },
  empty: { color: '#666', padding: 32, textAlign: 'center' as const },
}

const SENSITIVITY_COLOR: Record<string, string> = {
  local_only: '#ef4444',
  sanitized: '#facc15',
  public: '#22c55e',
}

const TIER_COLOR: Record<string, string> = {
  working: '#4a9eff',
  recall: '#9ca3af',
  archival: '#6b7280',
}

function formatLatency(ms: number): string {
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString()
}

export default function Traces() {
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TraceDetail | null>(null)
  const [filter, setFilter] = useState<string>('')
  const [archetype, setArchetype] = useState<string>('')
  const [trigger, setTrigger] = useState<string>('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // #34 — when set, drills into the prompt-construction inspector for the
  // currently-selected trace. Lives at /traces/:id/context conceptually
  // even though we don't use a router — the URL hash mirrors the route.
  const [inspectingId, setInspectingId] = useState<string | null>(null)

  // Read/write a #/traces/{id}/context fragment so the inspector deep-link
  // is shareable across reloads (the issue calls out the route name).
  useEffect(() => {
    const apply = () => {
      const m = window.location.hash.match(/^#\/traces\/([^/]+)\/context$/)
      if (m) {
        setInspectingId(m[1])
        setSelectedId(m[1])
      } else {
        setInspectingId(null)
      }
    }
    apply()
    window.addEventListener('hashchange', apply)
    return () => window.removeEventListener('hashchange', apply)
  }, [])

  const openInspector = (id: string) => {
    window.location.hash = `#/traces/${id}/context`
    setInspectingId(id)
    logInfo('traces', 'inspector_opened', { trace_id: id })
  }
  const closeInspector = () => {
    if (window.location.hash.startsWith('#/traces/')) {
      window.location.hash = ''
    }
    setInspectingId(null)
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .listTraces({
        limit: 50,
        archetype: archetype || undefined,
        triggerSource: trigger || undefined,
        containsText: filter || undefined,
      })
      .then((response) => {
        if (cancelled) return
        setTraces(response.traces)
        logInfo('traces', 'list_loaded', { count: response.traces.length })
      })
      .catch((e) => {
        if (cancelled) return
        const message = e instanceof Error ? e.message : String(e)
        setError(message)
        logError('traces', 'list_failed', { message })
      })
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [filter, archetype, trigger])

  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    let cancelled = false
    api
      .getTrace(selectedId)
      .then((d) => {
        if (cancelled) return
        setDetail(d)
        logInfo('traces', 'detail_loaded', { trace_id: selectedId })
      })
      .catch((e) => {
        if (cancelled) return
        const message = e instanceof Error ? e.message : String(e)
        logError('traces', 'detail_failed', { trace_id: selectedId, message })
      })
    return () => {
      cancelled = true
    }
  }, [selectedId])

  if (inspectingId) {
    return (
      <TraceContextInspector
        traceId={inspectingId}
        detail={detail && detail.trace_id === inspectingId ? detail : undefined}
        onBack={closeInspector}
      />
    )
  }

  return (
    <div style={styles.root}>
      <div style={styles.list}>
        <div style={styles.filters}>
          <input
            style={styles.input}
            placeholder="contains text…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <select
            style={styles.input}
            value={archetype}
            onChange={(e) => setArchetype(e.target.value)}
          >
            <option value="">all archetypes</option>
            <option value="orchestrator">orchestrator</option>
            <option value="reflector">reflector</option>
            <option value="monitor">monitor</option>
            <option value="researcher">researcher</option>
          </select>
          <select
            style={styles.input}
            value={trigger}
            onChange={(e) => setTrigger(e.target.value)}
          >
            <option value="">all triggers</option>
            <option value="user">user</option>
            <option value="scheduler">scheduler</option>
            <option value="agent">agent</option>
          </select>
        </div>
        <div style={styles.rows}>
          {loading && <div style={styles.empty}>loading…</div>}
          {error && !loading && (
            <div style={styles.empty}>error: {error}</div>
          )}
          {!loading && !error && traces.length === 0 && (
            <div style={styles.empty}>no traces yet</div>
          )}
          {traces.map((t) => (
            <div
              key={t.trace_id}
              style={styles.row(t.trace_id === selectedId)}
              onClick={() => setSelectedId(t.trace_id)}
            >
              <div>
                <span style={styles.pill(SENSITIVITY_COLOR[t.data_sensitivity] || '#888')}>
                  {t.data_sensitivity}
                </span>
                <span style={styles.pill(TIER_COLOR[t.tier] || '#888')}>{t.tier}</span>
                <span style={styles.pill('#a78bfa')}>{t.archetype}</span>
                {t.scheduler_job_name && (
                  <span style={styles.pill('#60a5fa')}>{t.scheduler_job_name}</span>
                )}
              </div>
              <div style={styles.rowMeta}>
                {formatTimestamp(t.created_at)} · {t.model_selected || '<no model>'} ·{' '}
                {formatLatency(t.latency_ms)}
              </div>
            </div>
          ))}
        </div>
      </div>
      <div style={styles.detail}>
        {!detail && <div style={styles.empty}>select a trace to inspect</div>}
        {detail && (
          <>
            <div style={styles.header}>{detail.trace_id}</div>
            <div style={styles.rowMeta}>
              {formatTimestamp(detail.created_at)} · {detail.model_selected} ·{' '}
              {formatLatency(detail.latency_ms)} · prompt:{' '}
              <code>{detail.prompt_version}</code>
            </div>
            <button
              style={styles.inspectButton}
              onClick={() => openInspector(detail.trace_id)}
            >
              Inspect prompt construction →
            </button>
            <div style={styles.field}>
              <div style={styles.label}>input</div>
              <div style={styles.value}>{detail.input}</div>
            </div>
            <div style={styles.field}>
              <div style={styles.label}>output</div>
              <div style={styles.value}>{detail.output}</div>
            </div>
            <div style={styles.field}>
              <div style={styles.label}>
                tools called ({detail.tools_called.length})
              </div>
              <div style={styles.value}>
                {JSON.stringify(detail.tools_called, null, 2)}
              </div>
            </div>
            <div style={styles.field}>
              <div style={styles.label}>assembled context</div>
              <div style={styles.value}>
                {JSON.stringify(detail.assembled_context, null, 2)}
              </div>
            </div>
            <div style={styles.field}>
              <div style={styles.label}>provenance</div>
              <div style={styles.value}>
                {JSON.stringify(
                  {
                    trigger_source: detail.trigger_source,
                    scheduler_job_name: detail.scheduler_job_name,
                    archetype: detail.archetype,
                    data_sensitivity: detail.data_sensitivity,
                    tier: detail.tier,
                    has_embedding: detail.has_embedding,
                    embedding_model_version: detail.embedding_model_version,
                    user_reaction: detail.user_reaction,
                  },
                  null,
                  2,
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
