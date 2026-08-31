import type {
  AuthSession,
  ChatAttachment,
  ChatResponse,
  ChatSession,
  MachineContext,
  ManualIndexStatus,
  ManualSearchResponse,
  DemoUser,
  MachineSummary,
  MaintenanceTicket,
  OrderRecord,
  QuoteSummary,
} from '../types/contracts'
import { createClientId } from '../clientId'

const API_BASE = (import.meta.env.VITE_GATEWAY_URL ?? '').replace(/\/+$/, '')

type StreamTrace = Pick<ChatResponse, 'agentTrace' | 'intents' | 'routingReason'>

let csrfToken: string | null = null

export class GatewayRequestError extends Error {
  status: number
  /**
   * The gateway's machine-readable reason, when it sent one.
   *
   * Without this the UI cannot tell "you may not see this" from "there is
   * nothing here" from "the source is down", and would offer a retry for a
   * request that can never succeed.
   */
  code?: string

  constructor(status: number, message = `Gateway request failed with status ${status}`, code?: string) {
    super(message)
    this.name = 'GatewayRequestError'
    this.status = status
    this.code = code
  }

  /** True when retrying could plausibly help. */
  get isRetryable() {
    return this.status === 408 || this.status === 429 || this.status >= 500
  }
}

/**
 * What each refusal means to the person holding the phone.
 *
 * Keyed by the gateway's `code`, never by its `message`. The message is written
 * for the log - "JWT is not active yet", "Demo auth cookie is required",
 * "Machine access is not allowed for this identity" - and a line operator on a
 * plant floor can do nothing with any of it. Naming the failing token field
 * also tells anyone probing the login exactly which check they tripped.
 */
const ERROR_MESSAGES: Record<string, string> = {
  // Sign-in
  session_expired: 'Your session has ended. Please sign in again.',
  csrf_required: 'Your session has ended. Please sign in again.',
  sign_in_failed: 'Sign-in could not be completed. Please try again.',
  sign_in_unavailable: 'Sign-in is temporarily unavailable. Please try again shortly.',
  demo_disabled: 'Sign-in by account picker is not enabled here.',
  invalid_user: 'That account was not recognised.',
  user_not_found: 'That account was not recognised.',

  // Access. These say a boundary was enforced, not that the data is missing -
  // the difference the dataset brief insists on.
  visibility_denied: 'Your role does not give you access to this information.',
  visibility_unknown: 'Your role does not give you access to this information.',
  machine_not_allowed: 'You do not have access to this machine.',
  machine_not_in_company: 'This machine belongs to another company.',
  company_unknown: 'Your account is not linked to a company yet.',

  // Missing
  machine_not_found: 'That machine is not in the fleet.',
  manual_not_found: 'No manual is available for this machine.',

  // Upstream
  upstream_unavailable: 'The assistant is temporarily unavailable. Please try again shortly.',
  upstream_stream_interrupted: 'The answer was cut short. Please ask again.',
  invalid_upstream_stream: 'The answer could not be read. Please ask again.',
  operational_commit_failed: 'The answer could not be recorded, so it was not completed.',

  // Configuration. An operator cannot fix this, so it says who can.
  server_misconfigured: 'The platform is not configured correctly. Contact AROL support.',
}

/**
 * An operator-facing sentence for a failed request.
 *
 * The server's own wording is deliberately never returned. It reaches the
 * browser on every error body, and printing it is what put "JWT is not active
 * yet" and an internal hostname in front of a plant operator.
 */
export function describeGatewayError(error: unknown): string {
  if (!(error instanceof GatewayRequestError)) {
    return 'Something went wrong. Please try again.'
  }
  const known = error.code ? ERROR_MESSAGES[error.code] : undefined
  if (known) {
    return known
  }
  switch (error.status) {
    case 401:
      return 'Your session has ended. Please sign in again.'
    case 403:
      return 'You do not have access to this.'
    case 404:
      return 'That was not found.'
    case 408:
      return 'That took too long. Please try again.'
    case 429:
      return 'Too many requests. Please wait a moment and try again.'
    case 502:
    case 503:
      return 'That data source is currently unavailable, so nothing can be shown for it.'
    default:
      return 'The request could not be completed.'
  }
}

async function errorFromResponse(response: Response, fallback?: string) {
  let message: string | undefined
  let code: string | undefined
  try {
    const body = await response.clone().json()
    if (body && typeof body === 'object') {
      if (typeof body.error === 'string') {
        message = body.error
      }
      if (typeof body.code === 'string') {
        code = body.code
      }
    }
  } catch {
    // A non-JSON body carries no reason; the status still does.
  }
  return new GatewayRequestError(response.status, message ?? fallback, code)
}

function rememberCsrfToken(payload: unknown) {
  if (payload && typeof payload === 'object' && 'csrfToken' in payload) {
    const nextToken = (payload as { csrfToken?: unknown }).csrfToken
    if (typeof nextToken === 'string' && nextToken) {
      csrfToken = nextToken
    }
  }
}

function isStateChanging(method?: string) {
  return !['GET', 'HEAD', 'OPTIONS'].includes((method ?? 'GET').toUpperCase())
}

function isCsrfBootstrapPath(path: string) {
  return path === '/api/v1/auth/session' || path === '/api/v1/auth/demo/login'
}

async function ensureCsrfToken() {
  if (csrfToken) {
    return
  }

  try {
    const response = await fetch(`${API_BASE}/api/v1/auth/session`, {
      credentials: 'include',
      headers: { Accept: 'application/json' },
    })

    if (response.ok) {
      rememberCsrfToken(await response.json())
    }
  } catch {
    // Auth-off local mode and offline startup do not need a CSRF token.
  }
}

async function buildHeaders(path: string, options?: RequestInit): Promise<Headers> {
  if (isStateChanging(options?.method) && !isCsrfBootstrapPath(path)) {
    await ensureCsrfToken()
  }

  const headers = options?.headers
  const merged = new Headers(headers)

  if (options?.body && !merged.has('Content-Type')) {
    merged.set('Content-Type', 'application/json')
  }

  if (csrfToken && isStateChanging(options?.method) && !merged.has('X-CSRF-Token')) {
    merged.set('X-CSRF-Token', csrfToken)
  }

  return merged
}

async function requestJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    ...options,
    headers: await buildHeaders(path, options),
  })

  if (!response.ok) {
    throw await errorFromResponse(response)
  }

  const payload = (await response.json()) as T
  rememberCsrfToken(payload)
  return payload
}

export function getMachineContext(machineId: string): Promise<MachineContext> {
  return requestJson<MachineContext>(`/api/v1/machines/${machineId}/context`)
}

export function getManualIndexStatus(): Promise<ManualIndexStatus> {
  return requestJson<ManualIndexStatus>('/api/v1/manuals/index/status')
}

export function searchManualIndex(
  machineId: string,
  query: string,
  limit = 3,
): Promise<ManualSearchResponse> {
  return requestJson<ManualSearchResponse>('/api/v1/manuals/search', {
    method: 'POST',
    body: JSON.stringify({ machineId, query, limit }),
  })
}

export function getAuthSession(): Promise<AuthSession> {
  return requestJson<AuthSession>('/api/v1/auth/session')
}

export function startOidcLogin(returnTo = `${window.location.pathname}${window.location.search}`): void {
  const query = new URLSearchParams({ returnTo })
  window.location.assign(`${API_BASE}/api/v1/auth/login?${query.toString()}`)
}

export function startProviderLogout(): void {
  window.location.assign(`${API_BASE}/api/v1/auth/logout/redirect`)
}

export function logoutAuthSession(
  returnTo = `${window.location.pathname}${window.location.search}`,
): Promise<AuthSession> {
  return requestJson<AuthSession>('/api/v1/auth/logout', {
    method: 'POST',
    body: JSON.stringify({ returnTo }),
  })
}

// A demo sign-in with no user named is deliberately not offered here. The
// gateway answers one with its configured default identity, and calling it on
// page load signed every visitor in as that user - full visibility over a real
// company - without anyone choosing it. Sign in through `signInAs` instead.

export function getChatSession(sessionId: string): Promise<ChatSession> {
  return requestJson<ChatSession>(`/api/v1/chat/sessions/${encodeURIComponent(sessionId)}`)
}

export async function createChatSession(
  machineId: string,
  idempotencyKey = createClientId(),
): Promise<ChatSession> {
  const session = await requestJson<ChatSession>('/api/v1/chat/sessions', {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify({ machineId, idempotencyKey }),
  })

  return {
    ...session,
    machineId: session.machineId ?? machineId,
    messages: session.messages ?? [],
  }
}

export function streamChatMessage(
  sessionId: string,
  machineId: string,
  message: string,
  attachments: ChatAttachment[],
  idempotencyKey: string,
  onToken: (delta: string) => void,
  onTrace?: (trace: StreamTrace) => void,
): Promise<ChatResponse> {
  return streamJsonSse(
    '/api/v1/chat/stream',
    { sessionId, machineId, message, attachments },
    idempotencyKey,
    onToken,
    onTrace,
  )
}

export function getGatewayAssetUrl(path: string): string {
  return `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`
}

async function streamJsonSse(
  path: string,
  body: unknown,
  idempotencyKey: string,
  onToken: (delta: string) => void,
  onTrace?: (trace: StreamTrace) => void,
): Promise<ChatResponse> {
  const controller = new AbortController()
  let timeoutId = 0
  const armIdleTimeout = () => {
    window.clearTimeout(timeoutId)
    timeoutId = window.setTimeout(() => controller.abort(), 60000)
  }
  armIdleTimeout()

  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      credentials: 'include',
      headers: await buildHeaders(path, {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
      }),
      body: JSON.stringify(body),
      signal: controller.signal,
    })

    if (!response.ok) {
      throw await errorFromResponse(response, `Gateway stream failed with status ${response.status}`)
    }

    if (!response.body) {
      throw new Error('Gateway stream did not return a readable body')
    }

    return await readChatStream(response.body, onToken, onTrace, armIdleTimeout)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error('Chat stream timed out waiting for activity', { cause: error })
    }

    throw error
  } finally {
    window.clearTimeout(timeoutId)
  }
}

async function readChatStream(
  body: ReadableStream<Uint8Array>,
  onToken: (delta: string) => void,
  onTrace?: (trace: StreamTrace) => void,
  onActivity?: () => void,
): Promise<ChatResponse> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finalResponse: ChatResponse | null = null

  while (true) {
    const { value, done } = await reader.read()

    if (done) {
      break
    }

    onActivity?.()
    buffer += decoder.decode(value, { stream: true })
    const events = buffer.split(/\r?\n\r?\n/)
    buffer = events.pop() ?? ''

    for (const event of events) {
      finalResponse = handleSseEvent(event, onToken, onTrace) ?? finalResponse
    }
  }

  buffer += decoder.decode()

  if (buffer.trim()) {
    finalResponse = handleSseEvent(buffer, onToken, onTrace) ?? finalResponse
  }

  if (!finalResponse) {
    throw new Error('Chat stream ended without a final response')
  }

  return finalResponse
}

function handleSseEvent(
  rawEvent: string,
  onToken: (delta: string) => void,
  onTrace?: (trace: StreamTrace) => void,
): ChatResponse | null {
  const event = parseSseEvent(rawEvent)

  if (!event) {
    return null
  }

  const payload = JSON.parse(event.data) as unknown

  if (event.name === 'token') {
    const token = payload as { delta?: string }

    if (token.delta) {
      onToken(token.delta)
    }

    return null
  }

  if (event.name === 'trace') {
    onTrace?.(payload as StreamTrace)
    return null
  }

  if (event.name === 'done') {
    return payload as ChatResponse
  }

  if (event.name === 'error') {
    const errorPayload = payload as { message?: string; error?: string }
    throw new Error(errorPayload.message ?? errorPayload.error ?? 'Chat stream failed')
  }

  return null
}

function parseSseEvent(rawEvent: string): { name: string; data: string } | null {
  let name = 'message'
  const data: string[] = []

  for (const line of rawEvent.split(/\r?\n/)) {
    if (line.startsWith('event:')) {
      name = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      data.push(line.slice('data:'.length).trimStart())
    }
  }

  return data.length > 0 ? { name, data: data.join('\n') } : null
}

// ---------------------------------------------------------------------------
// Fleet, identity and commercial reads
// ---------------------------------------------------------------------------

/** The dataset identities the demo can sign in as. */
export async function listDemoUsers(): Promise<{ users: DemoUser[] }> {
  return requestJson<{ users: DemoUser[] }>('/api/v1/auth/demo/users')
}

/** Sign in as a specific dataset user. */
export async function signInAs(userId: string): Promise<AuthSession> {
  return requestJson<AuthSession>('/api/v1/auth/demo/login', {
    method: 'POST',
    body: JSON.stringify({ userId }),
  })
}

/** The machines owned by the signed-in user's company. */
export async function getFleet(): Promise<{ machines: MachineSummary[] }> {
  return requestJson<{ machines: MachineSummary[] }>('/api/v1/machines')
}

export async function getQuotes(): Promise<{ quotes: QuoteSummary[] }> {
  return requestJson<{ quotes: QuoteSummary[] }>('/api/v1/quotes')
}

export async function getMachineOrders(machineId: string): Promise<OrderRecord[]> {
  return requestJson<OrderRecord[]>(`/api/v1/machines/${encodeURIComponent(machineId)}/orders`)
}

export async function getMaintenanceTickets(
  machineId: string,
): Promise<{ tickets: MaintenanceTicket[] }> {
  return requestJson<{ tickets: MaintenanceTicket[] }>(
    `/api/v1/machines/${encodeURIComponent(machineId)}/maintenance`,
  )
}
