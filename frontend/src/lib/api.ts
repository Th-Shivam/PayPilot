const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000'

export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL
).replace(/\/+$/, '')

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

export interface TraceStep {
  step_number: number
  step_name: string
  step_status: string
  step_result?: string | null
  detail?: Record<string, unknown>
}

export interface TraceMetadata {
  request_id: string
  run_id: string
  created_at: string
  steps: TraceStep[]
}

export interface ResolveResponse {
  txn_id: string
  transaction_id?: string | null
  status: string
  explanation: string
  action: string
  trace: TraceMetadata
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}/${path.replace(/^\/+/, '')}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...init.headers,
    },
  })

  if (!response.ok) {
    const message = await response.text()
    throw new ApiClientError(
      message || `Request failed with status ${response.status}`,
      response.status,
    )
  }

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
