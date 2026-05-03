// Epic 01 (#34) — Trace context inspector.
//
// Given a trace, breaks down what went into the prompt and why. Most
// sensitive view in the entire UI: surfaces raw life-context section
// names, raw memory IDs/scores, and (on re-render) the rendered system
// prompt. Localhost-bind is enforced server-side by `agent/traces/http.py`
// (see `_enforce_localhost_bind`), so this component just trusts the
// API guard and shows a banner so the operator never forgets.
//
// Why no react-router: the existing app uses a tab-based root. Adding
// a router for one drill-down adds a dependency for negligible benefit.
// Instead, the parent (Traces.tsx) owns selection state and switches to
// this component when "Inspect prompt construction" is clicked.

import { useEffect, useState } from 'react'
import {
  api,
  type MemoryDetail,
  type RerenderPromptResponse,
  type TraceDetail,
} from '../api'
import { logError, logInfo } from '../logger'

const styles = {
  root: {
    height: '100%',
    overflowY: 'auto' as const,
    padding: 24,
    fontFamily: 'inherit',
  },
  banner: {
    background: '#3a1212',
    border: '1px solid #ef4444',
    color: '#fecaca',
    borderRadius: 6,
    padding: '10px 14px',
    fontSize: 13,
    fontWeight: 600,
    marginBottom: 16,
    letterSpacing: 0.2,
  },
  legacyBanner: {
    background: '#1f2937',
    border: '1px solid #374151',
    color: '#cbd5e1',
    borderRadius: 6,
    padding: '8px 12px',
    fontSize: 12,
    marginBottom: 16,
  },
  topbar: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    marginBottom: 16,
  },
  backButton: {
    background: '#1a1a1a',
    color: '#e0e0e0',
    border: '1px solid #2a2a2a',
    borderRadius: 4,
    padding: '6px 12px',
    cursor: 'pointer',
    fontSize: 12,
    fontFamily: 'inherit',
  },
  title: { fontSize: 18, fontWeight: 600 },
  meta: { fontSize: 11, color: '#888' },
  section: {
    marginTop: 20,
    border: '1px solid #1e1e1e',
    borderRadius: 6,
    background: '#0d0d0d',
    overflow: 'hidden' as const,
  },
  sectionHeader: {
    padding: '10px 14px',
    background: '#141414',
    borderBottom: '1px solid #1e1e1e',
    fontSize: 13,
    fontWeight: 600,
    display: 'flex',
    justifyContent: 'space-between' as const,
    alignItems: 'center',
  },
  sectionBody: { padding: 14 },
  reason: {
    fontSize: 12,
    color: '#9ca3af',
    fontStyle: 'italic' as const,
    marginBottom: 10,
  },
  pre: {
    background: '#080808',
    color: '#e0e0e0',
    padding: 10,
    borderRadius: 4,
    border: '1px solid #1e1e1e',
    fontFamily: 'ui-monospace, monospace',
    fontSize: 12,
    whiteSpace: 'pre-wrap' as const,
    overflowX: 'auto' as const,
    margin: 0,
  },
  list: { margin: 0, paddingLeft: 18, fontSize: 13 },
  pill: (color: string) => ({
    display: 'inline-block',
    padding: '1px 6px',
    borderRadius: 3,
    fontSize: 10,
    background: `${color}22`,
    color,
    marginRight: 6,
  }),
  empty: { color: '#666', fontSize: 12 },
  rerenderRow: {
    display: 'flex',
    gap: 10,
    alignItems: 'center',
    marginTop: 8,
  },
  primaryButton: {
    background: '#1e3a5f',
    color: '#bfdbfe',
    border: '1px solid #2a4a73',
    borderRadius: 4,
    padding: '6px 12px',
    cursor: 'pointer',
    fontSize: 12,
    fontFamily: 'inherit',
  },
  matchPillTrue: {
    color: '#86efac',
    background: '#14532d44',
    padding: '2px 8px',
    borderRadius: 3,
    fontSize: 11,
  },
  matchPillFalse: {
    color: '#fca5a5',
    background: '#7f1d1d44',
    padding: '2px 8px',
    borderRadius: 3,
    fontSize: 11,
  },
  capabilityDiff: {
    fontSize: 12,
    color: '#fcd34d',
  },
  diffLineAdded: {
    color: '#86efac',
    background: '#14532d22',
    fontFamily: 'ui-monospace, monospace',
    fontSize: 12,
    whiteSpace: 'pre-wrap' as const,
    padding: '0 4px',
  },
  diffLineRemoved: {
    color: '#fca5a5',
    background: '#7f1d1d22',
    fontFamily: 'ui-monospace, monospace',
    fontSize: 12,
    whiteSpace: 'pre-wrap' as const,
    padding: '0 4px',
  },
  diffLineUnchanged: {
    color: '#9ca3af',
    fontFamily: 'ui-monospace, monospace',
    fontSize: 12,
    whiteSpace: 'pre-wrap' as const,
    padding: '0 4px',
  },
  diffContainer: {
    background: '#080808',
    border: '1px solid #1e1e1e',
    borderRadius: 4,
    padding: 8,
    marginTop: 8,
    maxHeight: 360,
    overflowY: 'auto' as const,
  },
  historyTurn: {
    borderTop: '1px solid #1a1a1a',
    padding: '8px 0',
    fontSize: 12,
  },
  historyHeader: {
    display: 'flex',
    gap: 8,
    alignItems: 'center',
    marginBottom: 4,
  },
  historyContent: {
    color: '#cbd5e1',
    whiteSpace: 'pre-wrap' as const,
    margin: 0,
    fontFamily: 'inherit',
    fontSize: 12,
  },
}

interface Props {
  traceId: string
  /** Optional pre-loaded detail; component refetches if missing. */
  detail?: TraceDetail | null
  onBack: () => void
}

interface ProvenanceShape {
  life_context_sections_used?: string[]
  last_n_turns?: number
  memory_ids?: Array<[string, number]>
  skill_match?: Record<string, unknown> | null
  capability_block_version?: string
  selectors?: Record<string, Record<string, unknown>>
}

function provenanceOf(detail: TraceDetail | null): ProvenanceShape {
  if (!detail) return {}
  return (detail.assembled_context || {}) as ProvenanceShape
}

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString()
}

function formatMemoryScore(score: number | undefined | null): string {
  if (typeof score !== 'number' || Number.isNaN(score)) return '—'
  return score.toFixed(3)
}

// Tiny, dependency-free line-by-line diff. Uses LCS via dynamic
// programming: O(n*m) time / space, fine for the typical ≤200-line
// inputs the inspector handles. Returns ordered diff lines.
type DiffOp = 'added' | 'removed' | 'unchanged'
interface DiffLine {
  op: DiffOp
  text: string
}

function lineDiff(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split('\n')
  const b = newText.split('\n')
  const n = a.length
  const m = b.length
  // dp[i][j] = LCS length of a[i:] and b[j:]
  const dp: number[][] = Array.from({ length: n + 1 }, () =>
    new Array(m + 1).fill(0),
  )
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      if (a[i] === b[j]) {
        dp[i][j] = dp[i + 1][j + 1] + 1
      } else {
        dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1])
      }
    }
  }
  const out: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ op: 'unchanged', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ op: 'removed', text: a[i] })
      i++
    } else {
      out.push({ op: 'added', text: b[j] })
      j++
    }
  }
  while (i < n) {
    out.push({ op: 'removed', text: a[i] })
    i++
  }
  while (j < m) {
    out.push({ op: 'added', text: b[j] })
    j++
  }
  return out
}

const DIFF_MAX_LINES = 50

interface DiffViewerProps {
  oldText: string
  newText: string
  /** Caption before the diff for context (e.g. "vs prior trace abc123"). */
  caption?: string
}

function DiffViewer({ oldText, newText, caption }: DiffViewerProps) {
  const diff = lineDiff(oldText, newText)
  const visible = diff.slice(0, DIFF_MAX_LINES)
  const omitted = diff.length - visible.length
  const anyChanges = diff.some((d) => d.op !== 'unchanged')
  return (
    <div style={styles.diffContainer}>
      {caption && (
        <div style={{ fontSize: 11, color: '#888', marginBottom: 6 }}>
          {caption}
        </div>
      )}
      {!anyChanges && (
        <div style={{ fontSize: 12, color: '#6b7280' }}>(no differences)</div>
      )}
      {visible.map((line, idx) => {
        const prefix = line.op === 'added' ? '+ ' : line.op === 'removed' ? '- ' : '  '
        const style =
          line.op === 'added'
            ? styles.diffLineAdded
            : line.op === 'removed'
              ? styles.diffLineRemoved
              : styles.diffLineUnchanged
        return (
          <div key={idx} style={style}>
            {prefix}
            {line.text}
          </div>
        )
      })}
      {omitted > 0 && (
        <div style={{ fontSize: 11, color: '#888', marginTop: 4 }}>
          … {omitted} more line(s) (truncated)
        </div>
      )}
    </div>
  )
}

interface ExpandableMemoryRowProps {
  id: string
  score: number
}

function ExpandableMemoryRow({ id, score }: ExpandableMemoryRowProps) {
  const [open, setOpen] = useState(false)
  const [memory, setMemory] = useState<MemoryDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Fetch on first expand. We don't refetch on subsequent toggles —
  // memory rows are append-only at the recall layer.
  useEffect(() => {
    if (!open || memory || loading) return
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .getMemory(id)
      .then((d) => {
        if (cancelled) return
        setMemory(d)
        logInfo('inspector', 'memory_loaded', { memory_id: id })
      })
      .catch((e) => {
        if (cancelled) return
        const message = e instanceof Error ? e.message : String(e)
        setError(message)
        logError('inspector', 'memory_failed', { memory_id: id, message })
      })
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [open, id, memory, loading])

  return (
    <li
      style={{
        listStyle: 'none',
        padding: '6px 0',
        borderBottom: '1px solid #1a1a1a',
      }}
    >
      <div
        style={{ cursor: 'pointer', display: 'flex', gap: 8, alignItems: 'center' }}
        onClick={() => setOpen(!open)}
      >
        <span style={{ color: '#888', fontSize: 11, width: 14 }}>{open ? '▼' : '▶'}</span>
        <code style={{ fontSize: 12 }}>{id}</code>
        <span style={{ marginLeft: 'auto', color: '#9ca3af', fontSize: 11 }}>
          score: {formatMemoryScore(score)}
        </span>
      </div>
      {open && (
        <div
          style={{
            marginTop: 6,
            marginLeft: 22,
            padding: '8px 10px',
            background: '#080808',
            border: '1px solid #1e1e1e',
            borderRadius: 4,
            color: '#9ca3af',
            fontSize: 12,
          }}
        >
          {loading && <div>loading memory…</div>}
          {error && (
            <div style={{ color: '#fca5a5' }}>error: {error}</div>
          )}
          {memory && (
            <>
              <div style={{ fontSize: 11, color: '#888', marginBottom: 4 }}>
                type: <code>{memory.type}</code> · importance:{' '}
                <code>{formatMemoryScore(memory.importance_score)}</code> ·
                created: <code>{formatTimestamp(memory.created_at)}</code>
              </div>
              {/* React auto-escapes; <pre> renders raw text safely. */}
              <pre style={styles.pre}>{memory.content}</pre>
              {memory.summary && (
                <>
                  <div style={{ fontSize: 11, color: '#888', marginTop: 6 }}>
                    summary:
                  </div>
                  <pre style={styles.pre}>{memory.summary}</pre>
                </>
              )}
            </>
          )}
        </div>
      )}
    </li>
  )
}

interface HistoryViewProps {
  history: Array<{ role?: string; content?: string; timestamp?: string }>
}

function HistoryView({ history }: HistoryViewProps) {
  if (!Array.isArray(history) || history.length === 0) {
    return <div style={styles.empty}>no history available</div>
  }
  // Per-turn timestamps: LastNTurnsSelector currently does not put
  // per-message timestamps in provenance — provenance is count-based
  // for now. We render whatever is present and explain when timestamps
  // are absent. We never fabricate them.
  const anyTimestamps = history.some((h) => h && typeof h.timestamp === 'string')
  return (
    <>
      {!anyTimestamps && (
        <div style={{ ...styles.empty, marginBottom: 6 }}>
          (timestamps unavailable for this trace — provenance currently
          records counts only)
        </div>
      )}
      <div>
        {history.map((turn, i) => {
          const role = turn?.role ?? '?'
          const ts = turn?.timestamp
          const content = turn?.content ?? ''
          return (
            <div key={i} style={styles.historyTurn}>
              <div style={styles.historyHeader}>
                <span style={styles.pill('#60a5fa')}>{role}</span>
                {ts && (
                  <span style={{ fontSize: 11, color: '#9ca3af' }}>
                    ({formatTimestamp(ts)})
                  </span>
                )}
              </div>
              <pre style={styles.historyContent}>{content}</pre>
            </div>
          )
        })}
      </div>
    </>
  )
}

export default function TraceContextInspector({ traceId, detail: detailProp, onBack }: Props) {
  const [detail, setDetail] = useState<TraceDetail | null>(detailProp ?? null)
  const [loading, setLoading] = useState(!detailProp)
  const [error, setError] = useState<string | null>(null)
  const [rerender, setRerender] = useState<RerenderPromptResponse | null>(null)
  const [rerendering, setRerendering] = useState(false)
  const [rerenderError, setRerenderError] = useState<string | null>(null)

  useEffect(() => {
    if (detailProp) {
      setDetail(detailProp)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .getTrace(traceId)
      .then((d) => {
        if (cancelled) return
        setDetail(d)
        logInfo('inspector', 'detail_loaded', { trace_id: traceId })
      })
      .catch((e) => {
        if (cancelled) return
        const message = e instanceof Error ? e.message : String(e)
        setError(message)
        logError('inspector', 'detail_failed', { trace_id: traceId, message })
      })
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [traceId, detailProp])

  const handleRerender = async () => {
    setRerendering(true)
    setRerenderError(null)
    try {
      const result = await api.rerenderPrompt(traceId)
      setRerender(result)
      logInfo('inspector', 'rerender_succeeded', {
        trace_id: traceId,
        matches_original: result.matches_original,
      })
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setRerenderError(message)
      logError('inspector', 'rerender_failed', { trace_id: traceId, message })
    } finally {
      setRerendering(false)
    }
  }

  if (loading) {
    return <div style={styles.root}>loading…</div>
  }
  if (error || !detail) {
    return (
      <div style={styles.root}>
        <button style={styles.backButton} onClick={onBack}>
          ← back
        </button>
        <div style={{ marginTop: 16, color: '#fca5a5' }}>
          error: {error ?? 'trace not found'}
        </div>
      </div>
    )
  }

  const prov = provenanceOf(detail)
  const reasons = detail.decision_reasons || {}
  const sections = prov.life_context_sections_used || []
  const memoryIds = prov.memory_ids || []
  const lifeContextContent =
    (prov.selectors?.life_context as { content?: string } | undefined)?.content ?? null
  const lastNTurnsProvenance = (prov.selectors?.last_n_turns ?? {}) as Record<string, unknown>
  const historyContent = (lastNTurnsProvenance.content ?? null) as
    | Array<{ role?: string; content?: string; timestamp?: string }>
    | null
  const provenanceIsLegacy = Object.keys(prov).length === 0

  return (
    <div style={styles.root}>
      <div style={styles.banner}>
        Raw personal data — local only. This panel inlines life-context
        sections and memory IDs from the active database. Localhost bind
        is enforced server-side; never expose this UI on a public network.
      </div>

      {provenanceIsLegacy && (
        <div style={styles.legacyBanner}>
          This trace pre-dates the assembly-provenance feature (#33).
          Upgrade to a newer trace to inspect prompt construction.
        </div>
      )}

      <div style={styles.topbar}>
        <button style={styles.backButton} onClick={onBack}>
          ← back to trace
        </button>
        <div>
          <div style={styles.title}>Prompt construction</div>
          <div style={styles.meta}>
            trace <code>{detail.trace_id}</code> ·{' '}
            {formatTimestamp(detail.created_at)} · {detail.model_selected || '<no model>'}
            {detail.prompt_version && (
              <>
                {' · prompt: '}
                <code>{detail.prompt_version}</code>
              </>
            )}
          </div>
        </div>
      </div>

      {/* ── Life context ──────────────────────────────────────────── */}
      <div style={styles.section}>
        <div style={styles.sectionHeader}>
          <span>Life context</span>
          <span style={styles.pill('#a78bfa')}>{sections.length} section(s)</span>
        </div>
        <div style={styles.sectionBody}>
          {reasons.life_context && <div style={styles.reason}>{reasons.life_context}</div>}
          {sections.length === 0 && <div style={styles.empty}>no sections recorded</div>}
          {sections.length > 0 && (
            <ul style={styles.list}>
              {sections.map((s) => (
                <li key={s}>
                  <code>{s}</code>
                </li>
              ))}
            </ul>
          )}
          {lifeContextContent && (
            <pre style={{ ...styles.pre, marginTop: 10 }}>{lifeContextContent}</pre>
          )}
        </div>
      </div>

      {/* ── Conversation history ──────────────────────────────────── */}
      <div style={styles.section}>
        <div style={styles.sectionHeader}>
          <span>Conversation history</span>
          <span style={styles.pill('#60a5fa')}>last {prov.last_n_turns ?? 0} turn(s)</span>
        </div>
        <div style={styles.sectionBody}>
          {reasons.last_n_turns && <div style={styles.reason}>{reasons.last_n_turns}</div>}
          {historyContent && Array.isArray(historyContent) ? (
            <HistoryView history={historyContent} />
          ) : (
            <div style={styles.empty}>
              Provenance records the count and limit only; raw turn text isn't
              persisted on the trace row to keep history mutations append-only.
              The original turn was emitted at{' '}
              <code>{formatTimestamp(detail.created_at)}</code>.
            </div>
          )}
        </div>
      </div>

      {/* ── Retrieved memories ────────────────────────────────────── */}
      <div style={styles.section}>
        <div style={styles.sectionHeader}>
          <span>Retrieved memories</span>
          <span style={styles.pill('#34d399')}>{memoryIds.length} hit(s)</span>
        </div>
        <div style={styles.sectionBody}>
          {reasons.retrieved_memory && (
            <div style={styles.reason}>{reasons.retrieved_memory}</div>
          )}
          {memoryIds.length === 0 ? (
            <div style={styles.empty}>no recall hits for this turn</div>
          ) : (
            <ul style={{ ...styles.list, paddingLeft: 0 }}>
              {memoryIds.map(([id, score], i) => (
                <ExpandableMemoryRow key={`${id}-${i}`} id={String(id)} score={Number(score)} />
              ))}
            </ul>
          )}
        </div>
      </div>

      {/* ── Skill match ───────────────────────────────────────────── */}
      <div style={styles.section}>
        <div style={styles.sectionHeader}>
          <span>Skill match</span>
          <span style={styles.pill('#facc15')}>
            {prov.skill_match ? 'matched' : 'no per-turn match'}
          </span>
        </div>
        <div style={styles.sectionBody}>
          {reasons.skill_match && <div style={styles.reason}>{reasons.skill_match}</div>}
          {prov.skill_match ? (
            <pre style={styles.pre}>{JSON.stringify(prov.skill_match, null, 2)}</pre>
          ) : (
            <div style={styles.empty}>
              No per-turn similarity match — the system uses progressive
              disclosure: skills are listed in the system prompt and the
              model picks them via the <code>skill_view</code> tool.
            </div>
          )}
        </div>
      </div>

      {/* ── Capability block ──────────────────────────────────────── */}
      <CapabilityBlockSection
        version={prov.capability_block_version ?? ''}
        reason={reasons.capability_block}
        capabilityProvenance={prov.selectors?.capability_block ?? null}
        currentTraceId={detail.trace_id}
      />

      {/* ── Re-render ─────────────────────────────────────────────── */}
      <div style={styles.section}>
        <div style={styles.sectionHeader}>
          <span>Re-render prompt</span>
          {rerender && (
            <span
              style={
                rerender.matches_original
                  ? styles.matchPillTrue
                  : styles.matchPillFalse
              }
            >
              {rerender.matches_original
                ? 'structural match'
                : 'diverged from original'}
            </span>
          )}
        </div>
        <div style={styles.sectionBody}>
          <div style={styles.reason}>
            Runs the live assembler against this trace's input. Result is
            in-browser only — never logged server-side.
          </div>
          <div style={styles.rerenderRow}>
            <button
              style={styles.primaryButton}
              onClick={handleRerender}
              disabled={rerendering}
            >
              {rerendering ? 'rendering…' : 'Re-render prompt'}
            </button>
            {rerender && (
              <span style={{ fontSize: 11, color: '#888' }}>
                hash: <code>{rerender.prompt_hash.slice(0, 12)}…</code>
              </span>
            )}
          </div>
          {rerenderError && (
            <div style={{ marginTop: 10, color: '#fca5a5', fontSize: 12 }}>
              error: {rerenderError}
            </div>
          )}
          {rerender && (
            <RerenderResult rerender={rerender} />
          )}
        </div>
      </div>
    </div>
  )
}

interface RerenderResultProps {
  rerender: RerenderPromptResponse
}

function RerenderResult({ rerender }: RerenderResultProps) {
  // The original full prompt isn't stored on the trace row today (the
  // trace stores input + output, not the rendered system prompt). When
  // we have no original prompt to diff against, we fall back to a
  // structural diff between the original provenance (persisted) and the
  // re-rendered provenance (fresh) so the operator still gets a useful
  // change view.
  const newProvenancePretty = JSON.stringify(rerender.provenance, null, 2)
  const oldProvenancePretty = JSON.stringify(rerender.original_provenance, null, 2)

  return (
    <>
      {rerender.notes.length > 0 && (
        <ul
          style={{
            ...styles.list,
            marginTop: 10,
            color: '#9ca3af',
            fontSize: 12,
          }}
        >
          {rerender.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
      <div style={{ marginTop: 10, fontSize: 11, color: '#888' }}>
        rendered prompt:
      </div>
      <pre style={styles.pre}>{rerender.prompt}</pre>
      <div style={{ marginTop: 10, fontSize: 11, color: '#888' }}>
        provenance diff (original → re-rendered) — the trace row stores
        provenance only, not the original rendered prompt, so this is the
        structural diff:
      </div>
      <DiffViewer
        oldText={oldProvenancePretty}
        newText={newProvenancePretty}
        caption="original provenance → re-rendered provenance"
      />
    </>
  )
}

interface CapabilityBlockSectionProps {
  version: string
  reason?: string
  capabilityProvenance: Record<string, unknown> | null
  currentTraceId: string
}

function CapabilityBlockSection({
  version,
  reason,
  capabilityProvenance,
  currentTraceId,
}: CapabilityBlockSectionProps) {
  const [priorVersion, setPriorVersion] = useState<string | null>(null)
  const [priorTraceId, setPriorTraceId] = useState<string | null>(null)
  const [priorContent, setPriorContent] = useState<string | null>(null)
  const [searched, setSearched] = useState(false)

  const currentContent =
    (capabilityProvenance?.content as string | undefined) ?? ''

  useEffect(() => {
    let cancelled = false
    // Find the most recent trace with a different capability_block_version
    // using ONLY the projected list view — no per-summary detail fetch
    // (#34 review fix: was N+1). The list response now carries
    // ``capability_block_version`` directly.
    setSearched(false)
    setPriorVersion(null)
    setPriorTraceId(null)
    setPriorContent(null)
    api
      .listTraces({ limit: 50 })
      .then(async (resp) => {
        let foundSummary: { trace_id: string; capability_block_version?: string | null } | null = null
        for (const summary of resp.traces) {
          if (cancelled) return
          if (summary.trace_id === currentTraceId) continue
          const ver = summary.capability_block_version
          if (ver && ver !== version) {
            foundSummary = summary
            break
          }
        }
        if (cancelled || !foundSummary) return
        setPriorVersion(foundSummary.capability_block_version ?? null)
        setPriorTraceId(foundSummary.trace_id)
        // ONE detail fetch — only for the chosen prior trace, to pull
        // its capability block content for the diff.
        try {
          const d = await api.getTrace(foundSummary.trace_id)
          if (cancelled) return
          const priorProv =
            (d.assembled_context as ProvenanceShape | undefined)?.selectors
              ?.capability_block
          const content =
            (priorProv as { content?: string } | undefined)?.content ?? null
          setPriorContent(content)
        } catch {
          // ignore — we'll show version-only when the detail fetch fails
        }
      })
      .catch(() => {
        // ignore — we'll just show "no prior version found"
      })
      .finally(() => !cancelled && setSearched(true))
    return () => {
      cancelled = true
    }
  }, [currentTraceId, version])

  return (
    <div style={styles.section}>
      <div style={styles.sectionHeader}>
        <span>Capability block</span>
        <code style={{ fontSize: 11, color: '#9ca3af' }}>{version || '<none>'}</code>
      </div>
      <div style={styles.sectionBody}>
        {reason && <div style={styles.reason}>{reason}</div>}
        {currentContent && (
          <>
            <div style={{ fontSize: 11, color: '#888', marginTop: 4 }}>
              current rendered block:
            </div>
            <pre style={styles.pre}>{currentContent}</pre>
          </>
        )}
        <div style={{ ...styles.capabilityDiff, marginTop: 10 }}>
          {!searched && 'searching for prior version…'}
          {searched && priorVersion && (
            <>
              prior trace <code>{priorTraceId}</code> ran with version{' '}
              <code>{priorVersion}</code>
              {priorVersion === version
                ? ' — unchanged'
                : ' — changed since last trace'}
            </>
          )}
          {searched && !priorVersion && (
            <span style={{ color: '#888' }}>
              no prior trace with a different version found in the last 50
              entries
            </span>
          )}
        </div>
        {searched && priorVersion && currentContent && (
          priorContent !== null ? (
            <DiffViewer
              oldText={priorContent}
              newText={currentContent}
              caption={`vs prior trace ${priorTraceId} (${priorVersion} → ${version})`}
            />
          ) : (
            <div style={{ ...styles.empty, marginTop: 8 }}>
              prior trace's capability block content not available (legacy
              row) — version mismatch only
            </div>
          )
        )}
      </div>
    </div>
  )
}
