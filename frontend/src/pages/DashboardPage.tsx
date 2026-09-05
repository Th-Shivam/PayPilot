import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react'
import Spline from '@splinetool/react-spline'
import {
  AlertTriangle,
  ArrowRight,
  BarChart3,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
  Clock3,
  Database,
  FileBarChart,
  Inbox,
  LayoutDashboard,
  ListFilter,
  MessageSquareText,
  MoreHorizontal,
  RefreshCw,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  Ticket,
  UserRound,
  X,
} from 'lucide-react'
import { Bar, BarChart, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Sidebar, SidebarBody, SidebarLink, useSidebar } from '../components/ui/sidebar'
import { PayPilotMark } from '../components/PayPilotMark'
import {
  ApiClientError,
  askAgent,
  getAnalytics,
  getTickets,
  getTrace,
  resolveTransaction,
  type AnalyticsResponse,
  type TicketRecord,
  type TraceMetadata,
} from '../lib/api'
import './dashboard.css'

type Role = 'support-agent' | 'business-owner'
type DashboardView = 'overview' | 'tickets' | 'investigations' | 'exceptions' | 'transactions' | 'reports'
type TicketViewStatus = 'Open' | 'Investigating' | 'Resolved' | 'Needs Review'
type EvidenceState = 'verified' | 'missing' | 'pending'

interface NavigationItem {
  id: DashboardView
  label: string
  icon: ReactElement
}

interface EvidenceItem {
  source: 'Gateway' | 'Bank' | 'Ledger'
  state: EvidenceState
  detail: string
}

interface AgentMessage {
  id: string
  role: 'user' | 'agent'
  text: string
  evidence?: EvidenceItem[]
}

const PAGE_SIZE = 7

const STATUS_COLORS: Record<TicketViewStatus, string> = {
  Open: '#d39a46',
  Investigating: '#7396d7',
  Resolved: '#6da989',
  'Needs Review': '#c77662',
}

const STATUS_OPTIONS: Array<'all' | TicketViewStatus> = ['all', 'Open', 'Investigating', 'Resolved', 'Needs Review']
const CHART_STATUSES: TicketViewStatus[] = ['Open', 'Investigating', 'Resolved', 'Needs Review']
const PAYPILOT_SCENE = 'https://prod.spline.design/33yYGDJAvjqUzUiE/scene.splinecode'
const PAYPILOT_SCENE_DURATION_MS = 10000

function ticketId(ticket: TicketRecord): string {
  if (ticket.ticket_id) return ticket.ticket_id
  const transaction = ticket.txn_id || ticket.transaction_id || 'UNKNOWN'
  return `TKT-${transaction.replace(/\D/g, '').slice(-4).padStart(4, '0')}`
}

function transactionId(ticket: TicketRecord): string {
  return ticket.txn_id || ticket.transaction_id || 'Unknown transaction'
}

function transactionIdFromMessage(message: string): string | null {
  // Live fixture IDs are both delimiter-free (TXNCLEAN001) and delimited
  // (TXN_CLEAN001/TXN-CLEAN001). Keep the compact form bounded at whitespace
  // so words such as "status" are never sent as part of the database key.
  const compact = message.match(/\bTXN[_-]?[A-Z0-9]+\b/i)
  if (compact) return compact[0].toUpperCase()

  // Also accept the readable form "TXN CLEAN 001" used in chat messages.
  const spaced = message.match(/\bTXN\s+([A-Z]+)\s+(\d+)\b/i)
  return spaced ? `TXN${spaced[1]}${spaced[2]}`.toUpperCase() : null
}

function diagnosis(ticket: TicketRecord): string {
  return ticket.diagnosis || ticket.status || 'unknown'
}

function viewStatus(ticket: TicketRecord): TicketViewStatus {
  const value = diagnosis(ticket)
  if (ticket.action_taken === 'escalated' || value === 'anomaly' || value === 'amount_mismatch' || value === 'unknown') return 'Needs Review'
  if (value === 'clean') return 'Resolved'
  if (value === 'pending') return 'Investigating'
  return 'Open'
}

function confidence(ticket: TicketRecord): string {
  if (typeof ticket.confidence === 'number') return ticket.confidence >= 0.7 ? 'high' : 'low_flagged_for_review'
  return ticket.confidence || 'unknown'
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }).format(date)
}

function humanize(value: string | null | undefined): string {
  if (!value) return 'Not recorded'
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function evidenceForTicket(ticket: TicketRecord): EvidenceItem[] {
  const status = diagnosis(ticket)
  return [
    { source: 'Gateway', state: 'verified', detail: 'Captured' },
    { source: 'Bank', state: status === 'anomaly' ? 'missing' : status === 'pending' ? 'pending' : 'verified', detail: status === 'anomaly' ? 'Record missing' : status === 'pending' ? 'Pending settlement' : 'Settled' },
    { source: 'Ledger', state: status === 'ledger_gap' ? 'missing' : 'verified', detail: status === 'ledger_gap' ? 'Entry missing' : 'Reconciled' },
  ]
}

function evidenceCounts(items: EvidenceItem[]): string {
  const verified = items.filter((item) => item.state === 'verified').length
  const missing = items.filter((item) => item.state === 'missing').length
  return `${items.length} sources checked · ${verified} verified · ${missing} missing`
}

function LoadingConsole(): ReactElement {
  return <div className="console-loading"><div className="loading-line loading-line-heading" /><div className="loading-stats"><span /><span /><span /><span /></div><div className="loading-panels"><span /><span /></div><div className="loading-table" /></div>
}

function BlockedConsole({ permissionDenied, message, onRetry }: { permissionDenied: boolean; message: string; onRetry: () => void }): ReactElement {
  const restricted = permissionDenied && message === 'Permission denied.'
  return <div className="blocked-console"><ShieldCheck size={22} /><p className="eyebrow">{restricted ? 'Permission denied' : 'Read unavailable'}</p><h2>{restricted ? 'This workspace is not available to your role.' : 'The operations feed could not be read.'}</h2><p>{restricted ? 'Ask a workspace owner for access to tickets and investigation traces.' : message}</p>{!restricted && <button type="button" className="console-button" onClick={onRetry}><RefreshCw size={14} /> Retry</button>}</div>
}

function StatusBadge({ status }: { status: TicketViewStatus }): ReactElement {
  const Icon = status === 'Resolved' ? CheckCircle2 : status === 'Needs Review' ? AlertTriangle : status === 'Investigating' ? Clock3 : CircleDashed
  return <span className={`status-badge status-${status.toLowerCase().replaceAll(' ', '-')}`}><Icon size={12} />{status}</span>
}

function EvidenceRows({ items }: { items: EvidenceItem[] }): ReactElement {
  return <div className="evidence-rows">{items.map((item) => <div className="evidence-row" key={item.source}><span className="evidence-source">{item.source}</span><span className={`evidence-state evidence-state-${item.state}`}>{item.state === 'verified' ? <Check size={12} /> : item.state === 'missing' ? <AlertTriangle size={12} /> : <Clock3 size={12} />}{item.detail}</span></div>)}</div>
}

function SidebarContents({ role, activeView, exceptionCount, onRoleChange, onNavigate }: { role: Role; activeView: DashboardView; exceptionCount: number; onRoleChange: (role: Role) => void; onNavigate: (view: DashboardView) => void }): ReactElement {
  const { open } = useSidebar()
  const supportLinks: NavigationItem[] = [
    { id: 'overview', label: 'Overview', icon: <LayoutDashboard size={18} /> },
    { id: 'tickets', label: 'Tickets', icon: <Ticket size={18} /> },
    { id: 'investigations', label: 'Investigations', icon: <Search size={18} /> },
    { id: 'exceptions', label: 'Exceptions', icon: <AlertTriangle size={18} /> },
  ]
  const ownerLinks: NavigationItem[] = [
    { id: 'overview', label: 'Overview', icon: <LayoutDashboard size={18} /> },
    { id: 'transactions', label: 'Transactions', icon: <Database size={18} /> },
    { id: 'tickets', label: 'Tickets', icon: <Ticket size={18} /> },
    { id: 'reports', label: 'Reports', icon: <FileBarChart size={18} /> },
  ]
  const links = role === 'support-agent' ? supportLinks : ownerLinks
  return <div className="sidebar-content"><div className="sidebar-top">{open ? <a className="sidebar-brand" href="/dashboard"><PayPilotMark /><span>PayPilot</span></a> : <a className="sidebar-brand sidebar-brand-collapsed" href="/dashboard"><PayPilotMark /></a>}{open && <div className="sidebar-role"><span className="sidebar-caption">Workspace role</span><div className="sidebar-role-control"><UserRound size={14} /><select value={role} onChange={(event) => onRoleChange(event.target.value as Role)} aria-label="Workspace role"><option value="support-agent">Support Agent</option><option value="business-owner">Business Owner</option></select><ChevronDown size={13} /></div></div>}<nav className="sidebar-nav" aria-label="Workspace navigation">{links.map((link) => <div className="sidebar-link-row" key={link.id}><SidebarLink link={{ label: link.label, href: '#', icon: link.icon, onClick: () => onNavigate(link.id) }} active={activeView === link.id} />{open && link.id === 'exceptions' && exceptionCount > 0 && <span className="sidebar-count">{exceptionCount}</span>}</div>)}</nav></div><div className="sidebar-bottom"><div className={`sidebar-user ${open ? '' : 'sidebar-user-collapsed'}`}><span className="sidebar-avatar">AM</span>{open && <span><strong>Alex Morgan</strong><small>{role === 'support-agent' ? 'Support Agent' : 'Business Owner'}</small></span>}</div></div></div>
}

function TicketTable({ tickets, onOpen, compact = false }: { tickets: TicketRecord[]; onOpen: (ticket: TicketRecord) => void; compact?: boolean }): ReactElement {
  if (tickets.length === 0) return <div className="table-empty"><Inbox size={20} /><strong>No tickets in this view.</strong><span>New reconciliation outcomes will appear here.</span></div>
  return <div className="ticket-table-wrap"><table className="ticket-table"><thead><tr><th>Ticket</th><th>Transaction</th><th>Status</th><th>Reason</th><th>Owner</th><th>Timestamp</th><th /></tr></thead><tbody>{tickets.map((ticket) => <tr key={ticketId(ticket)} onClick={() => onOpen(ticket)}><td><button type="button" className="ticket-link" onClick={(event) => { event.stopPropagation(); onOpen(ticket) }}>{ticketId(ticket)}<ArrowRight size={13} /></button></td><td className="transaction-cell">{transactionId(ticket)}</td><td><StatusBadge status={viewStatus(ticket)} /></td><td className="reason-cell" title={ticket.explanation}>{ticket.reason_code ? humanize(ticket.reason_code) : ticket.explanation || 'Not recorded'}</td><td className="owner-cell"><UserRound size={12} />{ticket.owner_id || 'Unassigned'}</td><td className="timestamp-cell">{formatDate(ticket.updated_at || ticket.created_at)}</td><td><button type="button" className="table-more" aria-label={`Open ${ticketId(ticket)}`} title="Open ticket" onClick={(event) => { event.stopPropagation(); onOpen(ticket) }}><MoreHorizontal size={16} /></button></td></tr>)}</tbody></table>{compact && <span className="table-compact-note">Showing latest investigations</span>}</div>
}

function TicketDetail({ ticket, trace, traceLoading, traceError, onClose }: { ticket: TicketRecord; trace: TraceMetadata | null; traceLoading: boolean; traceError: string | null; onClose: () => void }): ReactElement {
  const evidence = evidenceForTicket(ticket)
  return <div className="detail-layer"><button className="detail-backdrop" type="button" aria-label="Close ticket detail" onClick={onClose} /><aside className="ticket-detail"><header className="detail-header"><div><span className="eyebrow">Ticket detail</span><h2>{ticketId(ticket)}</h2></div><button type="button" className="detail-close" aria-label="Close ticket detail" title="Close detail" onClick={onClose}><X size={17} /></button></header><div className="detail-content"><div className="detail-status-line"><StatusBadge status={viewStatus(ticket)} /><span className="detail-confidence">{confidence(ticket) === 'low_flagged_for_review' ? 'Flagged for review' : 'High confidence'}</span></div><section className="detail-block"><h3>Case summary</h3><dl className="detail-grid"><div><dt>Transaction ID</dt><dd>{transactionId(ticket)}</dd></div><div><dt>Owner</dt><dd>{ticket.owner_id || 'Unassigned'}</dd></div><div><dt>Created at</dt><dd>{formatDate(ticket.created_at)}</dd></div><div><dt>Resolved at</dt><dd>{formatDate(ticket.resolved_at)}</dd></div><div className="detail-grid-wide"><dt>Reason</dt><dd>{ticket.reason_code ? humanize(ticket.reason_code) : 'Not recorded'}</dd></div></dl><p className="detail-resolution">{ticket.explanation || 'No resolution details have been recorded.'}</p></section><section className="detail-block"><div className="detail-block-heading"><h3>Evidence</h3><span><ShieldCheck size={13} /> Grounded sources</span></div><EvidenceRows items={evidence} /></section><section className="detail-block"><div className="detail-block-heading"><h3>Similar cases</h3><span><Sparkles size={13} /> Historical context</span></div><div className="similar-list"><div><span>01</span><p>Same diagnosis pattern found in prior reconciliation records.</p></div><div><span>02</span><p>Resolution guidance is limited to the evidence stored for this transaction.</p></div></div></section><section className="detail-block"><div className="detail-block-heading"><h3>Action history</h3><span><MessageSquareText size={13} /> Audit trail</span></div>{traceLoading ? <div className="detail-muted"><RefreshCw size={13} className="spin" /> Loading trace...</div> : traceError ? <div className="detail-muted detail-warning">{traceError}</div> : trace?.steps.length ? <ol className="trace-list">{trace.steps.map((step) => <li key={`${step.step_number}-${step.step_name}`}><span>{String(step.step_number).padStart(2, '0')}</span><div><strong>{humanize(step.step_name)}</strong><small>{step.step_result || humanize(step.step_status)}</small></div></li>)}</ol> : <div className="detail-muted">No trace has been recorded for this ticket.</div>}</section><section className="detail-block"><div className="detail-block-heading"><h3>Resolution details</h3><span><CheckCircle2 size={13} /> {humanize(ticket.action_taken)}</span></div><p className="detail-resolution">{ticket.explanation || 'The agent has not added a resolution note.'}</p></section></div></aside></div>
}

function AgentPanel({ tickets, selectedTicket, onOpenTicket, onClose }: { tickets: TicketRecord[]; selectedTicket: TicketRecord | null; onOpenTicket: (ticket: TicketRecord) => void; onClose: () => void }): ReactElement {
  const [messages, setMessages] = useState<AgentMessage[]>([{ id: 'welcome', role: 'agent', text: 'I trace gateway, bank and ledger records before answering. Ask me about a ticket or transaction.' }])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [sceneRun, setSceneRun] = useState(0)
  const [scenePlaying, setScenePlaying] = useState(false)
  const sceneTimer = useRef<number | null>(null)

  useEffect(() => () => {
    if (sceneTimer.current !== null) window.clearTimeout(sceneTimer.current)
  }, [])

  useEffect(() => {
    if (!selectedTicket) return
    const id = `ticket-${ticketId(selectedTicket)}`
    const timer = window.setTimeout(() => {
      setMessages((current) => current.some((message) => message.id === id) ? current : [...current, { id, role: 'agent', text: `I have the context for ${ticketId(selectedTicket)}. Ask me what changed or why it remains open.`, evidence: evidenceForTicket(selectedTicket) }])
    }, 0)
    return () => window.clearTimeout(timer)
  }, [selectedTicket])

  const sendMessage = async () => {
    const text = input.trim()
    if (!text || sending) return
    setInput('')
    setMessages((current) => [...current, { id: `user-${Date.now()}`, role: 'user', text }])
    setSending(true)
    setSceneRun((current) => current + 1)
    setScenePlaying(true)
    if (sceneTimer.current !== null) window.clearTimeout(sceneTimer.current)
    sceneTimer.current = window.setTimeout(() => setScenePlaying(false), PAYPILOT_SCENE_DURATION_MS)
    const history = messages
      .filter((message) => message.id !== 'welcome')
      .slice(-8)
      .map((message) => ({ role: message.role === 'agent' ? 'assistant' as const : 'user' as const, content: message.text }))
    const match = tickets.find((ticket) => text.toLowerCase().includes(ticketId(ticket).toLowerCase()) || text.toLowerCase().includes(transactionId(ticket).toLowerCase()))
    let responseTicket = match
    let resolvedText = ''
    let lookupError = ''
    const transactionMatch = transactionIdFromMessage(text)
    if (responseTicket) {
      try {
        const response = await askAgent(text, history, responseTicket as unknown as Record<string, unknown>)
        resolvedText = response.answer
      } catch {
        // The ticket row is still real data, so keep its stored explanation as
        // a deterministic fallback if the conversational model is unavailable.
        resolvedText = responseTicket.explanation
      }
    } else if (transactionMatch) {
      try {
        const response = await resolveTransaction(transactionMatch)
        responseTicket = { txn_id: transactionMatch, diagnosis: response.status, explanation: response.explanation, action_taken: response.action, confidence: 'high' }
        resolvedText = response.explanation
      } catch (error) {
        lookupError = error instanceof Error ? error.message : 'I could not retrieve a grounded record for that transaction.'
      }
    } else {
      try {
        const response = await askAgent(text, history)
        resolvedText = response.answer
      } catch (error) {
        lookupError = error instanceof Error ? error.message : 'The conversational model is unavailable right now.'
      }
    }
    await new Promise((resolve) => window.setTimeout(resolve, 280))
    if (!responseTicket) setMessages((current) => [...current, { id: `agent-${Date.now()}`, role: 'agent', text: resolvedText || lookupError || 'Give me a ticket ID or transaction ID from the register. I will only answer from the linked gateway, bank and ledger evidence.' }])
    else {
      const evidence = evidenceForTicket(responseTicket)
      setMessages((current) => [...current, { id: `agent-${Date.now()}`, role: 'agent', text: `I traced ${transactionId(responseTicket)} across the gateway, bank and ledger records. ${resolvedText || responseTicket.explanation || 'The available records do not include a resolution explanation.'}`, evidence }])
      if (match) onOpenTicket(match)
    }
    setSending(false)
  }

  return <section className="agent-panel"><header className="agent-header"><div className="agent-title"><span className="agent-mark"><Sparkles size={16} /></span><div><h2>PayPilot Agent</h2><span className="agent-online"><span /> Online</span></div></div><div className="agent-header-actions"><button type="button" className="panel-icon-button" title="Clear conversation" aria-label="Clear conversation" onClick={() => setMessages([{ id: 'welcome', role: 'agent', text: 'I trace gateway, bank and ledger records before answering. Ask me about a ticket or transaction.' }])}><RefreshCw size={15} /></button><button type="button" className="panel-icon-button" title="Close PayPilot Agent" aria-label="Close PayPilot Agent" onClick={onClose}><X size={15} /></button></div><p>Ask about a ticket or transaction.</p></header><div className="agent-conversation">{messages.map((message) => <div className={`agent-message agent-message-${message.role}`} key={message.id}><span className="message-label">{message.role === 'user' ? 'You' : 'PayPilot'}</span><p>{message.text}</p>{message.evidence && <div className="message-evidence"><EvidenceRows items={message.evidence} /><span className="evidence-counts">{evidenceCounts(message.evidence)}</span></div>}</div>)}{sending && <div className="agent-message agent-message-agent"><span className="message-label">PayPilot</span><p className="agent-typing"><span /><span /><span /></p></div>}</div>{scenePlaying && <div className="agent-spline-stage" aria-label="PayPilot is investigating" role="status"><Spline key={sceneRun} scene={PAYPILOT_SCENE} /><span className="agent-spline-label">PayPilot is tracing the evidence</span></div>}<form className="agent-composer" onSubmit={(event) => { event.preventDefault(); void sendMessage() }}><textarea value={input} onChange={(event) => setInput(event.target.value)} placeholder="Ask PayPilot about a transaction..." rows={2} /><button type="submit" aria-label="Send message" title="Send message" disabled={sending || !input.trim()}><Send size={16} /></button></form></section>
}

function AgentHeroButton({ onClick }: { onClick: () => void }): ReactElement {
  return <button type="button" className="agent-hero-button" aria-label="Open PayPilot Agent" onClick={onClick}><span className="agent-hero-edge agent-hero-edge-left" /><span className="agent-hero-edge agent-hero-edge-right" /><span className="agent-hero-edge agent-hero-edge-top" /><span className="agent-hero-edge agent-hero-edge-bottom" /><span className="agent-hero-button-content"><Sparkles size={15} /> PayPilot Agent</span></button>
}

export function DashboardPage(): ReactElement {
  const [role, setRole] = useState<Role>(() => window.localStorage.getItem('paypilot-role') === 'business-owner' ? 'business-owner' : 'support-agent')
  const [activeView, setActiveView] = useState<DashboardView>('overview')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [tickets, setTickets] = useState<TicketRecord[]>([])
  const [analytics, setAnalytics] = useState<AnalyticsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [permissionDenied, setPermissionDenied] = useState(false)
  const [apiError, setApiError] = useState('')
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | TicketViewStatus>('all')
  const [page, setPage] = useState(1)
  const [selectedTicket, setSelectedTicket] = useState<TicketRecord | null>(null)
  const [trace, setTrace] = useState<TraceMetadata | null>(null)
  const [traceLoading, setTraceLoading] = useState(false)
  const [traceError, setTraceError] = useState<string | null>(null)
  const [agentOpen, setAgentOpen] = useState(false)

  const loadData = useCallback(async () => {
    setLoading(true)
    setApiError('')
    setPermissionDenied(false)
    try {
      const [ticketResponse, analyticsResponse] = await Promise.all([getTickets(), getAnalytics()])
      setTickets(ticketResponse)
      setAnalytics(analyticsResponse)
    } catch (error) {
      const denied = error instanceof ApiClientError && (error.status === 401 || error.status === 403)
      // Keep the dashboard in a blocked state for every live-read failure.
      // A failed API call must never be replaced with fabricated ticket data.
      setPermissionDenied(true)
      setTickets([])
      setAnalytics(null)
      setApiError(error instanceof Error ? error.message : denied ? 'Permission denied.' : 'Live reads are unavailable.')
    } finally { setLoading(false) }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadData() }, 0)
    return () => window.clearTimeout(timer)
  }, [loadData])

  useEffect(() => {
    if (!selectedTicket) return undefined
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') setSelectedTicket(null) }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [selectedTicket])

  useEffect(() => {
    if (!agentOpen) return undefined
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') setAgentOpen(false) }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [agentOpen])

  const derivedAnalytics = useMemo<AnalyticsResponse>(() => {
    const byAction: Record<string, number> = {}
    const byConfidence: Record<string, number> = {}
    tickets.forEach((ticket) => { const action = ticket.action_taken || 'unknown'; const level = confidence(ticket); byAction[action] = (byAction[action] || 0) + 1; byConfidence[level] = (byConfidence[level] || 0) + 1 })
    return { by_action: byAction, by_confidence: byConfidence }
  }, [tickets])

  const statusCounts = useMemo(() => {
    const counts: Record<TicketViewStatus, number> = { Open: 0, Investigating: 0, Resolved: 0, 'Needs Review': 0 }
    tickets.forEach((ticket) => { counts[viewStatus(ticket)] += 1 })
    return counts
  }, [tickets])

  const chartData = CHART_STATUSES.map((status) => ({ name: status, count: statusCounts[status] }))
  const analyticsData = analytics || derivedAnalytics
  const totalTickets = tickets.length
  const reportedOutcomes = Object.values(analyticsData.by_action).reduce((total, value) => total + value, 0)
  const openTickets = totalTickets - statusCounts.Resolved
  const resolvedTickets = statusCounts.Resolved
  const latestInvestigations = tickets.slice(0, 5)

  const filteredTickets = useMemo(() => tickets.filter((ticket) => {
    const status = viewStatus(ticket)
    if (activeView === 'exceptions' && status !== 'Needs Review') return false
    if (activeView === 'investigations' && !['Investigating', 'Needs Review'].includes(status)) return false
    if (statusFilter !== 'all' && status !== statusFilter) return false
    const value = query.trim().toLowerCase()
    if (!value) return true
    return [ticketId(ticket), transactionId(ticket), ticket.reason_code, ticket.explanation, ticket.owner_id, status].filter(Boolean).join(' ').toLowerCase().includes(value)
  }), [activeView, query, statusFilter, tickets])

  const pageCount = Math.max(1, Math.ceil(filteredTickets.length / PAGE_SIZE))
  const safePage = Math.min(page, pageCount)
  const pageTickets = filteredTickets.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE)
  const exceptionCount = statusCounts['Needs Review']
  const handleRoleChange = (nextRole: Role) => { setRole(nextRole); window.localStorage.setItem('paypilot-role', nextRole); setActiveView('overview') }
  const handleOpenTicket = async (ticket: TicketRecord) => {
    setSelectedTicket(ticket)
    setTrace(null)
    setTraceError(null)
    setTraceLoading(true)
    try { setTrace(await getTrace(transactionId(ticket))) } catch (error) { setTraceError(error instanceof ApiClientError && (error.status === 401 || error.status === 403) ? 'Trace access is restricted for this workspace.' : 'Trace is not available for this transaction.') } finally { setTraceLoading(false) }
  }
  const viewTitle: Record<DashboardView, string> = { overview: 'Operations overview', tickets: 'Ticket register', investigations: 'Latest investigations', exceptions: 'Exception queue', transactions: 'Transaction register', reports: 'Operations reports' }

  return <div className="dashboard-viewport"><div className="dashboard-frame"><Sidebar open={sidebarOpen} setOpen={setSidebarOpen}><div className="dashboard-app"><SidebarBody className="reference-sidebar-body"><SidebarContents role={role} activeView={activeView} exceptionCount={exceptionCount} onRoleChange={handleRoleChange} onNavigate={(view) => { setActiveView(view); setPage(1) }} /></SidebarBody><main className="dashboard-main"><header className="dashboard-topbar"><div><span className="topbar-kicker">PayPilot / Workspace</span><h1>{viewTitle[activeView]}</h1></div><div className="topbar-actions"><span className="topbar-read"><span /> Supabase read</span><button type="button" className="topbar-refresh" aria-label="Refresh dashboard" title="Refresh dashboard" onClick={() => void loadData()}><RefreshCw size={15} /></button><span className="topbar-avatar">AM</span></div></header><div className="dashboard-content">{loading ? <LoadingConsole /> : permissionDenied ? <BlockedConsole permissionDenied message={apiError} onRetry={() => void loadData()} /> : <><div className="workspace-heading"><div><span className="eyebrow">{role === 'support-agent' ? 'Support operations' : 'Business pulse'}</span><div className="workspace-title-row"><h2>{role === 'support-agent' ? 'Keep the queue moving.' : 'Know where money is stuck.'}</h2><AgentHeroButton onClick={() => setAgentOpen(true)} /></div><p>{role === 'support-agent' ? 'Every ticket stays tied to the records that produced it.' : 'A concise view of reconciliation health across the workspace.'}</p></div><div className="workspace-date"><span className="live-dot" /> Live view <span>05 Sep 2026</span></div></div><div className="dashboard-columns"><section className="operations-panel"><div className="section-head"><div><span className="eyebrow">Operations / analytics</span><h3>{activeView === 'overview' ? 'At a glance' : viewTitle[activeView]}</h3></div><span className="section-meta">{totalTickets} tickets · {reportedOutcomes} reported outcomes</span></div><div className="compact-stats"><div><span>Open</span><strong>{openTickets}</strong><small>Needs a next action</small></div><div><span>Resolved</span><strong>{resolvedTickets}</strong><small>Closed with evidence</small></div><div><span>Investigating</span><strong>{statusCounts.Investigating}</strong><small>Still within the window</small></div><div><span>Needs review</span><strong>{statusCounts['Needs Review']}</strong><small>Human attention</small></div></div><div className="analytics-row"><div className="subpanel chart-subpanel"><div className="subpanel-head"><div><span className="eyebrow">Resolution breakdown</span><h4>Ticket status</h4></div><BarChart3 size={15} /></div><div className="bar-chart"><ResponsiveContainer width="100%" height={146}><BarChart data={chartData} margin={{ top: 14, right: 10, left: -12, bottom: 0 }}><XAxis dataKey="name" tick={{ fill: '#8a8a8a', fontSize: 10 }} axisLine={false} tickLine={false} /><YAxis allowDecimals={false} tick={{ fill: '#666', fontSize: 10 }} axisLine={false} tickLine={false} /><Tooltip cursor={{ fill: 'rgba(255,255,255,0.03)' }} contentStyle={{ background: '#242424', border: '1px solid #3f3f3f', borderRadius: 4, color: '#e9e9e9', fontSize: 11 }} /><Bar dataKey="count" isAnimationActive={false} radius={[2, 2, 0, 0]}>{chartData.map((entry) => <Cell key={entry.name} fill={STATUS_COLORS[entry.name]} />)}</Bar></BarChart></ResponsiveContainer></div></div><div className="subpanel distribution-subpanel"><div className="subpanel-head"><div><span className="eyebrow">Open vs resolved</span><h4>Queue health</h4></div><ListFilter size={15} /></div><div className="queue-health"><div className="queue-donut"><ResponsiveContainer width="100%" height="100%"><PieChart><Pie data={[{ name: 'Open', value: openTickets }, { name: 'Resolved', value: resolvedTickets }]} dataKey="value" isAnimationActive={false} innerRadius={32} outerRadius={50} paddingAngle={2} stroke="none">{[openTickets, resolvedTickets].map((_, index) => <Cell key={index} fill={index === 0 ? '#c77662' : '#6da989'} />)}</Pie></PieChart></ResponsiveContainer><div><strong>{totalTickets ? Math.round((resolvedTickets / totalTickets) * 100) : 0}%</strong><span>resolved</span></div></div><div className="queue-legend"><div><span className="legend-dot legend-dot-open" /> Open <strong>{openTickets}</strong></div><div><span className="legend-dot legend-dot-resolved" /> Resolved <strong>{resolvedTickets}</strong></div><div><span className="legend-dot legend-dot-review" /> Review <strong>{exceptionCount}</strong></div></div></div></div></div><div className="subpanel ticket-register"><div className="register-head"><div><span className="eyebrow">{activeView === 'overview' ? 'Latest investigations' : 'Operational register'}</span><h4>{activeView === 'overview' ? 'Recent tickets' : 'Tickets'}</h4></div><div className="register-actions"><label className="search-box"><Search size={14} /><span className="sr-only">Search tickets</span><input value={query} onChange={(event) => { setQuery(event.target.value); setPage(1) }} placeholder="Search ID" /></label><label className="filter-select"><span className="sr-only">Filter status</span><select value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value as 'all' | TicketViewStatus); setPage(1) }}>{STATUS_OPTIONS.map((status) => <option value={status} key={status}>{status === 'all' ? 'All statuses' : status}</option>)}</select><ChevronDown size={12} /></label></div></div><TicketTable tickets={activeView === 'overview' ? latestInvestigations : pageTickets} onOpen={(ticket) => void handleOpenTicket(ticket)} compact={activeView === 'overview'} /><div className="table-footer"><span>{activeView === 'overview' ? `${latestInvestigations.length} latest records` : `${filteredTickets.length === 0 ? 0 : (safePage - 1) * PAGE_SIZE + 1}-${Math.min(safePage * PAGE_SIZE, filteredTickets.length)} of ${filteredTickets.length}`}</span>{activeView !== 'overview' && <div><button type="button" className="table-page-button" aria-label="Previous page" title="Previous page" disabled={safePage <= 1} onClick={() => setPage((current) => Math.max(1, current - 1))}><ChevronLeft size={14} /></button><span>{safePage} / {pageCount}</span><button type="button" className="table-page-button" aria-label="Next page" title="Next page" disabled={safePage >= pageCount} onClick={() => setPage((current) => Math.min(pageCount, current + 1))}><ChevronRight size={14} /></button></div>}</div></div></section></div></>}</div><div className={`agent-drawer-layer${agentOpen ? ' agent-drawer-layer-open' : ''}`} aria-hidden={!agentOpen}><button type="button" className="agent-drawer-backdrop" aria-label="Close PayPilot Agent" onClick={() => setAgentOpen(false)} /><aside className="agent-drawer" aria-label="PayPilot Agent"><AgentPanel tickets={tickets} selectedTicket={selectedTicket} onOpenTicket={(ticket) => void handleOpenTicket(ticket)} onClose={() => setAgentOpen(false)} /></aside></div></main></div></Sidebar></div>{selectedTicket && <TicketDetail ticket={selectedTicket} trace={trace} traceLoading={traceLoading} traceError={traceError} onClose={() => setSelectedTicket(null)} />}</div>
}
