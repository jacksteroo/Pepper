import { ReactNode } from 'react'
import { logInfo } from '../logger'

type Tab = 'chat' | 'status' | 'context' | 'relationships' | 'traces' | 'alerts'

interface Props {
  tab: Tab
  onTabChange: (tab: Tab) => void
  children: ReactNode
}

const styles = {
  root: {
    display: 'flex',
    flexDirection: 'column' as const,
    height: '100vh',
    background: '#0d0d0d',
    color: '#e0e0e0',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    padding: '12px 20px',
    borderBottom: '1px solid #1e1e1e',
    background: '#111',
  },
  logo: {
    fontSize: '18px',
    fontWeight: 700,
    letterSpacing: '0.15em',
    color: '#4a9eff',
    marginRight: '8px',
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: '50%',
    background: '#22c55e',
    marginRight: 'auto',
  },
  tabs: {
    display: 'flex',
    gap: '4px',
  },
  tab: (active: boolean) => ({
    padding: '6px 16px',
    borderRadius: '6px',
    border: 'none',
    cursor: 'pointer',
    fontSize: '13px',
    fontFamily: 'inherit',
    background: active ? '#4a9eff22' : 'transparent',
    color: active ? '#4a9eff' : '#888',
    transition: 'all 0.15s',
  }),
  content: {
    flex: 1,
    overflow: 'hidden',
    display: 'flex',
    flexDirection: 'column' as const,
  },
}

export default function Layout({ tab, onTabChange, children }: Props) {
  return (
    <div style={styles.root}>
      <div style={styles.header}>
        <span style={styles.logo}>Pepper</span>
        <span style={styles.dot} title="online" />
        <nav style={styles.tabs}>
          {(['chat', 'status', 'context', 'relationships', 'traces', 'alerts'] as Tab[]).map((t) => (
            <button
              key={t}
              style={styles.tab(tab === t)}
              onClick={() => {
                logInfo('layout', 'tab_click', { currentTab: tab, nextTab: t })
                onTabChange(t)
              }}
            >
              {t === 'chat'
                ? 'Chat'
                : t === 'status'
                ? 'Status'
                : t === 'context'
                ? 'Life Context'
                : t === 'relationships'
                ? 'Relationships'
                : t === 'traces'
                ? 'Traces'
                : 'Alerts'}
            </button>
          ))}
        </nav>
      </div>
      <div style={styles.content}>{children}</div>
    </div>
  )
}
