const DEPLOYED_API_BASE_URL = 'https://paypilot-ki2b.onrender.com'

// Use the deployed API by default in every environment. Local development can
// still opt into a local backend through VITE_API_BASE_URL in .env.local.
const DEFAULT_API_BASE_URL = DEPLOYED_API_BASE_URL

export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || DEFAULT_API_BASE_URL
).replace(/\/+$/, '')

export type TraceEventType =
  | 'tool_start'
  | 'tool_result'
  | 'decision'
  | 'action'
  | 'retry'
  | 'completion'

export type TraceEventStatus =
  | 'running'
  | 'success'
  | 'warning'
  | 'not_found'
  | 'failed'
  | 'completed'

export interface TraceEvent {
  event_id: string
  transaction_id: string
  run_id: string
  request_id: string
  step_number: number
  event_type: TraceEventType
  step_name: string
  status: TraceEventStatus
  summary: string
  detail: Record<string, unknown>
  timestamp: string
  step_status?: string
  step_result?: string | null
}

export interface TraceMetadata {
  request_id: string
  run_id: string
  created_at: string
  steps: TraceEvent[]
}

export interface ResolveResponse {
  txn_id: string
  transaction_id?: string | null
  status: string
  explanation: string
  action: string
  trace: TraceMetadata
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ChatResponse {
  answer: string
}

export class ApiClientError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiClientError'
    this.status = status
  }
}

export interface TicketRecord {
  ticket_id?: string | null
  txn_id?: string | null
  transaction_id?: string | null
  diagnosis?: string | null
  status?: string | null
  reason_code?: string | null
  explanation: string
  action_taken: string
  confidence?: string | number | null
  detail?: Record<string, unknown> | null
  owner_id?: string | null
  created_at?: string | null
  updated_at?: string | null
  resolved_at?: string | null
}

export interface AnalyticsResponse {
  by_action: Record<string, number>
  by_confidence: Record<string, number>
}

async function fetchApi(path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers)
  if (!headers.has('Accept')) headers.set('Accept', 'application/json')
  return fetch(`${API_BASE_URL}/${path.replace(/^\/+/, '')}`, {
    ...init,
    headers,
  })
}

async function throwApiError(response: Response): Promise<never> {
  const fallback = `Request failed with status ${response.status}`
  let message = fallback
  try {
    const body = (await response.json()) as { error?: { message?: string } }
    message = body.error?.message || fallback
  } catch {
    const body = await response.text()
    if (body) message = body
  }
  throw new ApiClientError(message, response.status)
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetchApi(path, init)
  if (!response.ok) await throwApiError(response)
  return (await response.json()) as T
}

export function getTickets(): Promise<TicketRecord[]> {
  return apiFetch<TicketRecord[]>('tickets')
}

export function getAnalytics(): Promise<AnalyticsResponse> {
  return apiFetch<AnalyticsResponse>('analytics')
}

export function getTrace(transactionId: string): Promise<TraceMetadata> {
  return apiFetch<TraceMetadata>(`trace/${encodeURIComponent(transactionId)}`)
}

export function resolveTransaction(transactionId: string): Promise<ResolveResponse> {
  return apiFetch<ResolveResponse>('resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ txn_id: transactionId }),
  })
}

export function askAgent(message: string, history: ChatMessage[] = [], context?: Record<string, unknown>): Promise<ChatResponse> {
  return apiFetch<ChatResponse>('chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, history, context }),
  })
}

function parseSseFrame(frame: string): { event: string; id?: string; data: string } {
  let event = 'message'
  let id: string | undefined
  const data: string[] = []
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue
    const separator = line.indexOf(':')
    const field = separator === -1 ? line : line.slice(0, separator)
    const value = separator === -1 ? '' : line.slice(separator + 1).trimStart()
    if (field === 'event') event = value
    if (field === 'id') id = value
    if (field === 'data') data.push(value)
  }
  return { event, id, data: data.join('\n') }
}

function extractResolution(event: TraceEvent): ResolveResponse | null {
  if (event.event_type !== 'completion') return null
  const resolution = event.detail.resolution
  if (!resolution || typeof resolution !== 'object') return null
  const result = resolution as Record<string, unknown>
  return {
    txn_id: String(result.txn_id ?? event.transaction_id),
    transaction_id: String(result.transaction_id ?? event.transaction_id),
    status: String(result.status ?? 'unknown'),
    explanation: String(result.explanation ?? event.summary),
    action: String(result.action ?? 'no_action_needed'),
    trace: {
      request_id: event.request_id,
      run_id: event.run_id,
      created_at: event.timestamp,
      steps: [event],
    },
  }
}

export async function streamResolve(
  txnId: string,
  onEvent: (event: TraceEvent) => void,
  signal?: AbortSignal,
): Promise<ResolveResponse> {
  const response = await fetchApi('resolve', {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ txn_id: txnId }),
    signal,
  })
  if (!response.ok) await throwApiError(response)
  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('text/event-stream')) return (await response.json()) as ResolveResponse
  if (!response.body) throw new ApiClientError('The resolution stream did not include a body.', 502)

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let completion: ResolveResponse | null = null
  const consume = (frame: string): void => {
    const parsed = parseSseFrame(frame)
    if (!parsed.data) return
    if (parsed.event === 'error') {
      const error = JSON.parse(parsed.data) as { message?: string }
      throw new ApiClientError(error.message ?? 'Resolution stream failed.', 502)
    }
    const event = JSON.parse(parsed.data) as TraceEvent
    onEvent(event)
    completion = extractResolution(event) ?? completion
  }

  try {
    while (true) {
      const chunk = await reader.read()
      buffer += decoder.decode(chunk.value ?? new Uint8Array(), { stream: !chunk.done })
      let boundary = buffer.search(/\r?\n\r?\n/)
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary).replace(/^\r?\n\r?\n/, '')
        consume(frame)
        boundary = buffer.search(/\r?\n\r?\n/)
      }
      if (chunk.done) break
    }
    if (buffer.trim()) consume(buffer)
  } finally {
    reader.releaseLock()
  }
  if (!completion) throw new ApiClientError('Resolution stream ended before completion.', 502)
  return completion
}
