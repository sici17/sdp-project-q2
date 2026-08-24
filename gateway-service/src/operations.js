import { createHash, randomUUID } from 'node:crypto'
import path from 'node:path'
import { buildOperationsStore } from './operations-store.js'

const operationsDir = path.resolve(process.env.OPERATIONS_DATA_DIR ?? path.join('data', 'operations'))
const auditRetentionDays = Number(process.env.AUDIT_RETENTION_DAYS ?? 365)
const alertRetentionDays = Number(process.env.ALERT_RETENTION_DAYS ?? 90)
const operationsDatabasePath = path.resolve(
  process.env.OPERATIONS_DB_PATH ?? path.join(operationsDir, 'operations.sqlite'),
)
const operationsStore = await buildOperationsStore({
  operationsDir,
  databasePath: operationsDatabasePath,
})

const counters = {
  auditEvents: 0,
  alerts: new Map(),
  httpRequests: new Map(),
  operationsWriteFailures: new Map(),
}
const durationBuckets = [50, 100, 250, 500, 1000, 2500, 5000, 10000]
const durationHistograms = new Map()
const streamFirstTokenHistograms = new Map()

export async function operationsReadiness() {
  try {
    return await operationsStore.readiness()
  } catch (error) {
    return {
      status: 'unavailable',
      required: true,
      persistence: operationsStore.mode,
      error: error instanceof Error ? error.message : String(error),
    }
  }
}

export function recordHttpMetric({ method, pathname, statusCode, durationMs }) {
  const labels = {
    method,
    path: normalizeMetricPath(pathname),
    status: String(statusCode),
  }
  increment(counters.httpRequests, labelKey(labels))
  observeHistogram(durationHistograms, { method, path: labels.path }, durationMs)
}

export function recordStreamFirstTokenMetric(durationMs) {
  observeHistogram(streamFirstTokenHistograms, { stream: 'chat' }, durationMs)
}

export function recordOperationsWriteFailure(kind) {
  increment(counters.operationsWriteFailures, String(kind || 'unknown'))
}

export async function handleChatOperationalEvents({ request, response, requestId, authContext, mode }) {
  const operationKey = chatOperationKey(request, response, authContext, requestId)

  await recordAuditEvent({
    id: stableEventId('chat.completed', operationKey),
    type: 'chat.completed',
    requestId,
    actor: authActor(authContext),
    machineId: request.machineId,
    sessionId: request.sessionId,
    outcome: response.reviewRequired ? 'flagged' : 'completed',
    metadata: {
      mode,
      messageHash: sha256(request.message),
      agentTrace: response.agentTrace,
      intents: response.intents,
      answerConfidence: response.answerConfidence,
      reviewRequired: response.reviewRequired,
      reviewReasons: response.reviewReasons,
      toolStatuses: response.toolCalls?.map((toolCall) => ({
        name: toolCall.name,
        status: toolCall.status,
      })),
    },
  })
}

export async function recordEscalationAudit({ request, draft, requestId, authContext }) {
  await recordAuditEvent({
    type: 'escalation.drafted',
    requestId,
    actor: authActor(authContext),
    machineId: request.machineId,
    sessionId: request.sessionId,
    outcome: 'drafted',
    metadata: {
      messageHash: sha256(request.message),
      escalationId: draft.escalationId,
      evidenceCount: draft.evidence?.length ?? 0,
      recommendedActionCount: draft.recommendedActions?.length ?? 0,
    },
  })
}

export async function recordAuditEvent(event) {
  const entry = {
    id: randomUUID(),
    timestamp: new Date().toISOString(),
    ...event,
  }
  const created = await operationsStore.appendEvent('audit', entry, auditRetentionDays)
  if (created) {
    counters.auditEvents += 1
  }
  return entry
}

export async function recordAlert(alert) {
  const entry = {
    id: randomUUID(),
    timestamp: new Date().toISOString(),
    ...alert,
  }
  const created = await operationsStore.appendEvent('alert', entry, alertRetentionDays)
  if (created) {
    increment(counters.alerts, entry.severity)
    console.warn(JSON.stringify({ eventType: 'alert', ...entry }))
  }
  return entry
}

export async function createAuthSession(session) {
  return operationsStore.createAuthSession(session)
}

export async function getAuthSession(sessionId) {
  return operationsStore.getAuthSession(sessionId)
}

export async function deleteAuthSession(sessionId) {
  return operationsStore.deleteAuthSession(sessionId)
}

export async function createOidcLoginState(loginState) {
  return operationsStore.createOidcLoginState(loginState)
}

export async function consumeOidcLoginState(stateValue) {
  return operationsStore.consumeOidcLoginState(stateValue)
}

export async function createOidcLogoutState(logoutState) {
  return operationsStore.createOidcLogoutState(logoutState)
}

export async function getOidcLogoutState(stateValue) {
  return operationsStore.getOidcLogoutState(stateValue)
}

export async function consumeOidcLogoutState(stateValue) {
  return operationsStore.consumeOidcLogoutState(stateValue)
}

export async function listAuditEvents({ authContext, limit = 100 } = {}) {
  const events = await operationsStore.listEvents('audit', limit * 4)
  return events
    .filter((event) => !event.machineId || canSeeMachine(authContext, event.machineId))
    .slice(0, limit)
}

export async function listAlertEvents({ authContext, limit = 100 } = {}) {
  const events = await operationsStore.listEvents('alert', limit * 4)
  return events
    .filter((event) => !event.machineId || canSeeMachine(authContext, event.machineId))
    .slice(0, limit)
}

export async function prometheusMetrics() {
  const lines = [
    '# HELP arol_gateway_operations_storage_info Gateway operations storage mode (always 1).',
    '# TYPE arol_gateway_operations_storage_info gauge',
    `arol_gateway_operations_storage_info{mode="${operationsStore.mode}"} 1`,
    '# HELP arol_gateway_http_requests_total Gateway HTTP requests.',
    '# TYPE arol_gateway_http_requests_total counter',
    ...metricMapLines('arol_gateway_http_requests_total', counters.httpRequests),
    '# HELP arol_gateway_http_request_duration_ms Gateway HTTP request duration in milliseconds.',
    '# TYPE arol_gateway_http_request_duration_ms histogram',
    ...histogramMetricLines('arol_gateway_http_request_duration_ms', durationHistograms),
    '# HELP arol_gateway_stream_first_token_duration_ms Time until the first streamed AI token in milliseconds.',
    '# TYPE arol_gateway_stream_first_token_duration_ms histogram',
    ...histogramMetricLines(
      'arol_gateway_stream_first_token_duration_ms',
      streamFirstTokenHistograms,
    ),
    '# HELP arol_gateway_audit_events_total Audit events persisted by the gateway.',
    '# TYPE arol_gateway_audit_events_total counter',
    `arol_gateway_audit_events_total ${counters.auditEvents}`,
    '# HELP arol_gateway_alerts_total Alerts emitted by the gateway.',
    '# TYPE arol_gateway_alerts_total counter',
    ...metricMapLines('arol_gateway_alerts_total', counters.alerts, 'severity'),
    '# HELP arol_gateway_operations_write_failures_total Failed writes to gateway operational stores.',
    '# TYPE arol_gateway_operations_write_failures_total counter',
    ...metricMapLines(
      'arol_gateway_operations_write_failures_total',
      counters.operationsWriteFailures,
      'kind',
    ),
  ]

  return `${lines.join('\n')}\n`
}

function metricMapLines(name, values, singleLabelName) {
  const lines = []

  for (const [key, value] of values.entries()) {
    const labels = singleLabelName ? { [singleLabelName]: key } : JSON.parse(key)
    const labelText = Object.entries(labels)
      .map(([label, labelValue]) => `${label}="${String(labelValue).replaceAll('"', '\\"')}"`)
      .join(',')
    lines.push(`${name}{${labelText}} ${value}`)
  }

  return lines
}

function labelKey(labels) {
  return JSON.stringify(labels)
}

function increment(map, key, amount = 1) {
  map.set(key, (map.get(key) ?? 0) + amount)
}

function observeHistogram(map, labels, value) {
  const key = labelKey(labels)
  const histogram = map.get(key) ?? {
    buckets: new Map(durationBuckets.map((bucket) => [bucket, 0])),
    count: 0,
    sum: 0,
  }
  const durationMs = Math.max(0, Number(value) || 0)

  histogram.count += 1
  histogram.sum += durationMs
  for (const bucket of durationBuckets) {
    if (durationMs <= bucket) {
      histogram.buckets.set(bucket, histogram.buckets.get(bucket) + 1)
    }
  }
  map.set(key, histogram)
}

function histogramMetricLines(name, histograms) {
  const lines = []
  for (const [key, histogram] of histograms.entries()) {
    const labels = JSON.parse(key)
    for (const bucket of durationBuckets) {
      lines.push(metricLine(`${name}_bucket`, labels, histogram.buckets.get(bucket), { le: bucket }))
    }
    lines.push(metricLine(`${name}_bucket`, labels, histogram.count, { le: '+Inf' }))
    lines.push(metricLine(`${name}_count`, labels, histogram.count))
    lines.push(metricLine(`${name}_sum`, labels, histogram.sum))
  }
  return lines
}

function metricLine(name, labels, value, extraLabels = {}) {
  const labelText = Object.entries({ ...labels, ...extraLabels })
    .map(([label, labelValue]) => `${label}="${String(labelValue).replaceAll('"', '\\"')}"`)
    .join(',')
  return `${name}{${labelText}} ${value}`
}

function normalizeMetricPath(pathname) {
  return pathname
    .replace(/^\/api\/v1\/manuals\/index\/status$/, '/api/v1/manuals/index/status')
    .replace(/^\/api\/v1\/manuals\/search$/, '/api/v1/manuals/search')
    .replace(/\/api\/v1\/machines\/[^/]+/g, '/api/v1/machines/:machineId')
    .replace(/^\/api\/v1\/manuals\/[^/]+$/, '/api/v1/manuals/:machineId')
    .replace(/\/api\/v1\/chat\/sessions\/[^/]+/g, '/api/v1/chat/sessions/:sessionId')
}

function authActor(authContext) {
  return {
    subject: authContext?.subject ?? 'anonymous',
    mode: authContext?.mode ?? 'off',
    roles: authContext?.roles ?? [],
  }
}

function canSeeMachine(authContext, machineId) {
  return !authContext?.hasMachineRestriction || authContext.machineIds.has(machineId)
}

function sha256(value) {
  return createHash('sha256').update(String(value ?? '')).digest('hex')
}

function chatOperationKey(request, response, authContext, requestId) {
  const actor = authActor(authContext)
  const payloadHash = sha256(
    JSON.stringify({
      machineId: request.machineId,
      sessionId: request.sessionId,
      message: request.message,
      attachmentIds: request.attachments?.map((attachment) => attachment.id) ?? [],
    }),
  )
  // Idempotent requests need a stable key so replays repair side effects
  // without duplicating them. Non-idempotent turns are intentionally unique:
  // X-Request-Id is caller-controlled correlation metadata and is not an
  // acceptable deduplication key.
  const turnKey = request.idempotencyKey
    ? `idempotency:${request.idempotencyKey}:${payloadHash}`
    : `turn:${response.message?.id ?? 'unknown'}:${payloadHash}:${randomUUID()}`
  return `${actor.subject}:${request.machineId}:${request.sessionId}:${turnKey}`
}

function stableEventId(type, operationKey) {
  return `event-${sha256(`${type}:${operationKey}`).slice(0, 32)}`
}
