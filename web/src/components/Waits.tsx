/**
 * "Things Pepper chose not to surface" panel — Issue #55/#56.
 *
 * Shows recent wait-traces: timestamp, reason, until (if set).
 * Each entry has thumbs-up/down buttons that POST to /api/wait-feedback.
 * Style consistent with Traces.tsx.
 */
import { useEffect, useState } from 'react'
import { logError, logInfo } from '../logger'

const API = '/api'

interface WaitSummary {
  trace_id: string
  created_at: string
  reason: string
  until: string | null
  user_signal: 'correct' | 'incorrect' | null
}

async function listWaits(): Promise<WaitSummary[]> {
  const res = await fetch(`${API}/waits`, {
    headers: { 'Content-Type': 'application/json' },
  })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  const data = await res.json()
  return (data.waits ?? []) as WaitSummary[]
}

async function postFeedback(
  trace_id: string,
  user_signal: 'correct' | 'incorrect',
): Promise<void> {
  const res = await fetch(`${API}/wait-feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ wait_trace_id: trace_id, user_signal }),
  })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
}

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString()
}

const styles = {
  root: {
    display: 'flex',
    flexDirection: 'column' as const,
    height: '100%',
    overflow: 'hidden',
  },
  header: {
    padding: '16px 20px 8px',
    borderBottom: '1px solid #1e1e1e',
  },
  title: { fontSize: 16, fontWeight: 600, marginBottom: 4 },
  subtitle: { fontSize: 12, color: '#888' },
  list: { flex: 1, overflowY: 'auto' as const },
  row: {
    padding: '14px 20px',
    borderBottom: '1px solid #161616',
  },
  rowTop: {
    display: 'flex',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    gap: 8,
  },
  reason: {
    fontSize: 14,
    lineHeight: 1.5,
    flex: 1,
  },
  thumbs: {
    display: 'flex',
    gap: 6,
    flexShrink: 0,
  },
  thumbBtn: (active: boolean, color: string) => ({
    background: active ? `${color}33` : 'transparent',
    border: `1px solid ${active ? color : '#333'}`,
    color: active ? color : '#666',
    borderRadius: 4,
    padding: '2px 8px',
    cursor: 'pointer',
    fontSize: 14,
    fontFamily: 'inherit',
  }),
  meta: { fontSize: 11, color: '#888', marginTop: 4, display: 'flex', gap: 12 },
  until: {
    display: 'inline-block',
    background: '#1e3a1e',
    color: '#4ade80',
    borderRadius: 3,
    padding: '1px 6px',
    fontSize: 10,
    marginLeft: 6,
  },
  empty: { color: '#666', padding: 40, textAlign: 'center' as const },
  error: { color: '#ef4444', padding: 20, fontSize: 12 },
  loading: { color: '#888', padding: 20, fontSize: 12 },
}

export default function Waits() {
  const [waits, setWaits] = useState<WaitSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [signals, setSignals] = useState<Record<string, 'correct' | 'incorrect'>>({})

  useEffect(() => {
    setLoading(true)
    setError(null)
    listWaits()
      .then((w) => {
        setWaits(w)
        // Pre-populate known signals from the server.
        const pre: Record<string, 'correct' | 'incorrect'> = {}
        w.forEach((item) => {
          if (item.user_signal) pre[item.trace_id] = item.user_signal
        })
        setSignals(pre)
        logInfo('waits', 'list_loaded', { count: w.length })
      })
      .catch((e) => {
        const message = e instanceof Error ? e.message : String(e)
        setError(message)
        logError('waits', 'list_failed', { message })
      })
      .finally(() => setLoading(false))
  }, [])

  const handleThumb = (trace_id: string, signal: 'correct' | 'incorrect') => {
    postFeedback(trace_id, signal)
      .then(() => {
        setSignals((prev) => ({ ...prev, [trace_id]: signal }))
        logInfo('waits', 'feedback_sent', { trace_id, signal })
      })
      .catch((e) => {
        logError('waits', 'feedback_failed', { trace_id, signal, error: String(e) })
      })
  }

  return (
    <div style={styles.root}>
      <div style={styles.header}>
        <div style={styles.title}>Chose Not to Surface</div>
        <div style={styles.subtitle}>
          Times Pepper decided to wait rather than send a response.
          Thumbs = was this the right call?
        </div>
      </div>
      <div style={styles.list}>
        {loading && <div style={styles.loading}>Loading waits…</div>}
        {error && <div style={styles.error}>Error: {error}</div>}
        {!loading && !error && waits.length === 0 && (
          <div style={styles.empty}>No wait decisions recorded yet.</div>
        )}
        {waits.map((w) => {
          const currentSignal = signals[w.trace_id] ?? null
          return (
            <div key={w.trace_id} style={styles.row}>
              <div style={styles.rowTop}>
                <div style={styles.reason}>{w.reason}</div>
                <div style={styles.thumbs}>
                  <button
                    style={styles.thumbBtn(currentSignal === 'correct', '#22c55e')}
                    title="Was correct — right call to wait"
                    onClick={() => handleThumb(w.trace_id, 'correct')}
                  >
                    👍
                  </button>
                  <button
                    style={styles.thumbBtn(currentSignal === 'incorrect', '#ef4444')}
                    title="Was incorrect — should have surfaced this"
                    onClick={() => handleThumb(w.trace_id, 'incorrect')}
                  >
                    👎
                  </button>
                </div>
              </div>
              <div style={styles.meta}>
                <span>{formatTimestamp(w.created_at)}</span>
                {w.until && (
                  <span>
                    revisit
                    <span style={styles.until}>{w.until}</span>
                  </span>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
