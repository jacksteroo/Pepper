import { useEffect, useMemo, useState } from 'react'
import { api, ReflectorAlert } from '../api'
import { logError, logInfo } from '../logger'

type StatusFilter = 'open' | 'dismissed' | 'filed' | 'all'

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
  filterRow: {
    display: 'flex',
    gap: '8px',
    marginTop: '12px',
  },
  filterButton: (active: boolean) => ({
    padding: '6px 12px',
    borderRadius: '6px',
    border: '1px solid #2a2a2a',
    background: active ? '#4a9eff22' : 'transparent',
    color: active ? '#4a9eff' : '#888',
    cursor: 'pointer',
    fontSize: '12px',
    fontFamily: 'inherit',
  }),
  list: {
    flex: 1,
    overflow: 'auto',
    padding: '12px 20px',
  },
  alertCard: {
    background: '#111',
    border: '1px solid #1e1e1e',
    borderRadius: '6px',
    padding: '14px 16px',
    marginBottom: '12px',
  },
  alertHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'baseline',
    marginBottom: '6px',
  },
  summary: { fontSize: '13px', fontWeight: 600 },
  meta: { fontSize: '11px', color: '#777' },
  badge: (status: ReflectorAlert['status']) => ({
    display: 'inline-block',
    padding: '2px 8px',
    borderRadius: '4px',
    fontSize: '10px',
    fontWeight: 600,
    background:
      status === 'open' ? '#facc1522' : status === 'filed' ? '#22c55e22' : '#52525222',
    color:
      status === 'open' ? '#facc15' : status === 'filed' ? '#22c55e' : '#a1a1aa',
    marginLeft: '8px',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.05em',
  }),
  suggestion: {
    fontSize: '12px',
    color: '#aaa',
    marginTop: '8px',
    lineHeight: 1.5,
  },
  traceList: {
    display: 'flex',
    flexWrap: 'wrap' as const,
    gap: '6px',
    marginTop: '10px',
  },
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
  actions: {
    display: 'flex',
    gap: '8px',
    marginTop: '12px',
  },
  button: {
    padding: '6px 12px',
    borderRadius: '4px',
    border: '1px solid #2a2a2a',
    background: 'transparent',
    color: '#9ca3af',
    cursor: 'pointer',
    fontSize: '12px',
    fontFamily: 'inherit',
  },
  empty: {
    padding: '40px 20px',
    textAlign: 'center' as const,
    color: '#666',
    fontSize: '13px',
  },
  error: {
    padding: '16px',
    color: '#ef4444',
    fontSize: '12px',
  },
}

interface Props {
  /** Open a trace in the inspector. Caller is responsible for
   * switching to the Traces tab AND setting the URL hash; the
   * panel just calls back with the trace id. */
  onOpenTrace?: (traceId: string) => void
}

export default function ReflectorAlerts({ onOpenTrace }: Props = {}) {
  const [filter, setFilter] = useState<StatusFilter>('open')
  const [alerts, setAlerts] = useState<ReflectorAlert[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<Record<string, boolean>>({})

  const load = useMemo(
    () => async (which: StatusFilter) => {
      setError(null)
      try {
        const res = await api.listReflectorAlerts(which)
        setAlerts(res.alerts)
      } catch (exc) {
        logError('alerts', 'list_failed', { exc })
        setError(`Failed to load alerts: ${(exc as Error).message}`)
        setAlerts([])
      }
    },
    [],
  )

  useEffect(() => {
    logInfo('alerts', 'panel_mounted', { filter })
    void load(filter)
  }, [filter, load])

  const handleAction = async (
    alert: ReflectorAlert,
    action: 'dismiss' | 'file',
  ) => {
    setBusy((b) => ({ ...b, [alert.alert_id]: true }))
    try {
      if (action === 'dismiss') {
        await api.dismissReflectorAlert(alert.alert_id)
      } else {
        await api.fileReflectorAlert(alert.alert_id)
      }
      logInfo('alerts', 'status_changed', {
        alertId: alert.alert_id,
        action,
      })
      await load(filter)
    } catch (exc) {
      logError('alerts', 'status_change_failed', { exc, alertId: alert.alert_id })
      setError(`Action failed: ${(exc as Error).message}`)
    } finally {
      setBusy((b) => {
        const next = { ...b }
        delete next[alert.alert_id]
        return next
      })
    }
  }

  return (
    <div style={styles.root}>
      <div style={styles.header}>
        <div style={styles.title}>Reflector alerts</div>
        <div style={styles.hint}>
          Recurring failure-mode clusters surfaced by the nightly pattern
          detector. Each alert links to the underlying traces — review,
          then dismiss or mark as filed.
        </div>
        <div style={styles.filterRow}>
          {(['open', 'filed', 'dismissed', 'all'] as StatusFilter[]).map((f) => (
            <button
              key={f}
              style={styles.filterButton(filter === f)}
              onClick={() => setFilter(f)}
            >
              {f}
            </button>
          ))}
        </div>
      </div>
      {error && <div style={styles.error}>{error}</div>}
      <div style={styles.list}>
        {alerts === null ? (
          <div style={styles.empty}>Loading…</div>
        ) : alerts.length === 0 ? (
          <div style={styles.empty}>
            {filter === 'open'
              ? 'No open alerts. The pattern detector has not surfaced any recurring failures yet.'
              : `No ${filter} alerts.`}
          </div>
        ) : (
          alerts.map((a) => (
            <div key={a.alert_id} style={styles.alertCard}>
              <div style={styles.alertHeader}>
                <div style={styles.summary}>
                  {a.summary}
                  <span style={styles.badge(a.status)}>{a.status}</span>
                </div>
                <div style={styles.meta}>
                  conf {a.confidence.toFixed(2)} ·{' '}
                  {new Date(a.created_at).toLocaleString()}
                </div>
              </div>
              <div style={styles.suggestion}>{a.suggested_action}</div>
              <div style={styles.traceList}>
                {a.trace_ids.map((tid) => (
                  <a
                    key={tid}
                    style={styles.traceLink}
                    href={`#/traces/${tid}/context`}
                    onClick={(e) => {
                      e.preventDefault()
                      logInfo('alerts', 'trace_link_clicked', { traceId: tid })
                      if (onOpenTrace) {
                        onOpenTrace(tid)
                      } else {
                        // Fallback when the panel is rendered standalone
                        // (e.g. tests) — set the hash; the user must
                        // switch tabs manually.
                        window.location.hash = `#/traces/${tid}/context`
                      }
                    }}
                    title={tid}
                  >
                    {tid.slice(0, 8)}
                  </a>
                ))}
              </div>
              {a.status === 'open' && (
                <div style={styles.actions}>
                  <button
                    style={styles.button}
                    onClick={() => handleAction(a, 'file')}
                    disabled={!!busy[a.alert_id]}
                  >
                    File (took action)
                  </button>
                  <button
                    style={styles.button}
                    onClick={() => handleAction(a, 'dismiss')}
                    disabled={!!busy[a.alert_id]}
                  >
                    Dismiss
                  </button>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
