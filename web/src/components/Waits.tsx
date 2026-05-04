// Epic 06 (#55) — "Things Pepper chose not to surface".
//
// Read-only panel listing recent wait traces, newest first. Each entry
// shows the reason, the optional `until` value the model passed, and a
// chip linking back into the trace inspector so the operator can see
// the full assembled context that produced the wait.
import { useEffect, useState } from 'react'
import { api, type WaitEntry } from '../api'
import { logError, logInfo } from '../logger'

const styles = {
  root: {
    display: 'flex',
    flexDirection: 'column' as const,
    height: '100%',
    overflow: 'hidden',
    color: '#e0e0e0',
  },
  header: {
    padding: '16px 20px',
    borderBottom: '1px solid #1e1e1e',
    background: '#0d0d0d',
  },
  title: { fontSize: '14px', fontWeight: 600, marginBottom: '4px' },
  hint: { fontSize: '12px', color: '#888' },
  list: { flex: 1, overflow: 'auto', padding: '12px 20px' },
  card: {
    background: '#111',
    border: '1px solid #1e1e1e',
    borderRadius: '6px',
    padding: '14px 16px',
    marginBottom: '12px',
  },
  cardHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'baseline',
    marginBottom: '8px',
  },
  reason: {
    fontSize: '13px',
    lineHeight: 1.5,
    color: '#e0e0e0',
    whiteSpace: 'pre-wrap' as const,
  },
  meta: { fontSize: '11px', color: '#777' },
  pill: {
    display: 'inline-block',
    padding: '2px 8px',
    borderRadius: '4px',
    fontSize: '10px',
    fontWeight: 600,
    background: '#52525222',
    color: '#a1a1aa',
    marginLeft: '8px',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.05em',
  },
  detailsRow: {
    display: 'flex',
    flexWrap: 'wrap' as const,
    gap: '8px',
    marginTop: '10px',
    fontSize: '11px',
    color: '#888',
    alignItems: 'center' as const,
  },
  thumbButton: (active: boolean, accent: string) => ({
    padding: '3px 10px',
    background: active ? accent + '33' : 'transparent',
    color: active ? accent : '#9ca3af',
    border: `1px solid ${active ? accent : '#2a2a2a'}`,
    borderRadius: '4px',
    cursor: 'pointer',
    fontSize: '12px',
    fontFamily: 'inherit',
  }),
  traceLink: {
    fontSize: '11px',
    fontFamily: 'monospace',
    padding: '3px 8px',
    background: '#1f2937',
    color: '#9ca3af',
    borderRadius: '4px',
    textDecoration: 'none',
    cursor: 'pointer',
    border: '1px solid #2a2a2a',
  },
  empty: { color: '#666', padding: 32, textAlign: 'center' as const },
}

function formatRelative(iso: string): string {
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return iso
  const diffMs = Date.now() - dt.getTime()
  const mins = Math.round(diffMs / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 48) return `${hours}h ago`
  const days = Math.round(hours / 24)
  return `${days}d ago`
}

interface Props {
  onOpenTrace?: (traceId: string) => void
}

export default function Waits({ onOpenTrace }: Props) {
  const [waits, setWaits] = useState<WaitEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pendingThumb, setPendingThumb] = useState<string | null>(null)

  async function handleThumb(traceId: string, value: 'up' | 'down') {
    setPendingThumb(traceId)
    try {
      await api.thumbWait(traceId, value)
      setWaits((prev) =>
        prev
          ? prev.map((w) => (w.trace_id === traceId ? { ...w, thumb: value } : w))
          : prev,
      )
      logInfo('waits', 'thumb_recorded', { traceId, value })
    } catch (err) {
      logError('waits', 'thumb_failed', { traceId, error: String(err) })
    } finally {
      setPendingThumb(null)
    }
  }

  useEffect(() => {
    let cancelled = false
    logInfo('waits', 'mount')
    api
      .listWaits()
      .then((res) => {
        if (cancelled) return
        setWaits(res.waits)
        logInfo('waits', 'loaded', { count: res.waits.length })
      })
      .catch((err) => {
        if (cancelled) return
        setError(String(err))
        logError('waits', 'load_failed', { error: String(err) })
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (error) {
    return (
      <div style={styles.root}>
        <div style={styles.empty}>Failed to load waits: {error}</div>
      </div>
    )
  }

  if (waits === null) {
    return (
      <div style={styles.root}>
        <div style={styles.empty}>Loading…</div>
      </div>
    )
  }

  return (
    <div style={styles.root}>
      <div style={styles.header}>
        <div style={styles.title}>Things Pepper chose not to surface</div>
        <div style={styles.hint}>
          Each entry is a turn where Pepper called the <code>wait</code> tool. The
          reason is recorded with the trace; this panel surfaces the latest
          {waits.length > 0 ? ` ${waits.length}.` : '.'}
        </div>
      </div>
      <div style={styles.list}>
        {waits.length === 0 ? (
          <div style={styles.empty}>No waits recorded yet.</div>
        ) : (
          waits.map((w) => (
            <div key={w.trace_id} style={styles.card}>
              <div style={styles.cardHeader}>
                <div style={styles.meta}>
                  {formatRelative(w.created_at)}
                  <span style={styles.pill}>{w.trigger_source || 'turn'}</span>
                  {w.scheduler_job_name && (
                    <span style={styles.pill}>{w.scheduler_job_name}</span>
                  )}
                </div>
              </div>
              <div style={styles.reason}>{w.reason || <em>(no reason)</em>}</div>
              <div style={styles.detailsRow}>
                {w.until_raw && (
                  <span>
                    <strong>until:</strong> {w.until_raw}
                  </span>
                )}
                <span
                  style={styles.traceLink}
                  onClick={() => {
                    if (onOpenTrace) onOpenTrace(w.trace_id)
                  }}
                  title="Open trace in inspector"
                >
                  trace {w.trace_id.slice(0, 8)}
                </span>
                <button
                  style={styles.thumbButton(w.thumb === 'up', '#22c55e')}
                  disabled={pendingThumb === w.trace_id}
                  onClick={() => handleThumb(w.trace_id, 'up')}
                  title="Mark as a correct wait"
                >
                  ↑ correct
                </button>
                <button
                  style={styles.thumbButton(w.thumb === 'down', '#ef4444')}
                  disabled={pendingThumb === w.trace_id}
                  onClick={() => handleThumb(w.trace_id, 'down')}
                  title="Mark as an incorrect wait"
                >
                  ↓ incorrect
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
