import http from 'node:http'
import { createHash, randomUUID } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { access, readFile, stat } from 'node:fs/promises'
import path from 'node:path'
import { pipeline } from 'node:stream/promises'
import { URL, fileURLToPath } from 'node:url'
import {
  authRateLimitKey,
  authConfigurationReadiness,
  authenticateRequest,
  authorizeMachine,
  beginOidcLogin,
  beginOidcLogout,
  buildAuthConfig,
  completeOidcLogout,
  completeOidcLogin,
  createDemoJwt,
  csrfTokenForAuthToken,
  getBearerToken,
  getCookieAuthToken,
  getCookieValue,
  logoutAuthSession,
  oidcLogoutRedirect,
  requireCsrfForCookieAuth,
} from './auth.js'
import {
  getLatestTelemetry,
  getMachine,
  getMachines,
  getMachineAlarms,
  getMachineContext,
  getMachineRecord,
  getMachineTelemetryHistory,
  getManual,
  getManualAccessMachineIds,
  getManuals,
  UpstreamUnavailableError,
} from './data.js'
import {
  assertTenancy,
  assertVisibility,
  canSeeDomain,
  canSeeMachine,
  DOMAIN,
  domainsFor,
  isKnownVisibility,
  tenantCompanyId,
} from './access.js'
import { HttpError } from './http-error.js'
import {
  findUser,
  listMachines,
  listMaintenanceTickets,
  listOrders,
  listQuotes,
  listUsers,
} from './dataset.js'
import {
  handleChatOperationalEvents,
  listAlertEvents,
  listAuditEvents,
  prometheusMetrics,
  recordAlert,
  recordEscalationAudit,
  recordHttpMetric,
  recordOperationsWriteFailure,
  recordStreamFirstTokenMetric,
  operationsReadiness,
} from './operations.js'
import { createRateLimiter } from './rate-limit.js'

const port = Number(process.env.PORT ?? 8080)
const corsOrigin = process.env.CORS_ORIGIN ?? 'http://localhost:5173'
const maxBodyBytes = Number(process.env.MAX_BODY_BYTES ?? 65536)
const maxChatBodyBytes = Number(process.env.MAX_CHAT_BODY_BYTES ?? 16 * 1024 * 1024)
const maxAttachments = 4
const maxAttachmentBytes = 5 * 1024 * 1024
const maxTotalAttachmentBytes = 10 * 1024 * 1024
const idempotencyReplayed = Symbol('idempotencyReplayed')
const allowedAttachmentTypes = new Set([
  'application/pdf',
  'text/plain',
])
const aiServiceUrl = (process.env.AI_SERVICE_URL?.trim() || 'http://localhost:8000').replace(/\/+$/, '')
const aiServiceTimeoutMs = Number(process.env.AI_SERVICE_TIMEOUT_MS ?? 10000)
const aiServiceStreamConnectTimeoutMs = Number(
  process.env.AI_SERVICE_STREAM_CONNECT_TIMEOUT_MS ?? 10000,
)
const aiServiceStreamIdleTimeoutMs = Number(
  process.env.AI_SERVICE_STREAM_IDLE_TIMEOUT_MS
    ?? process.env.AI_SERVICE_STREAM_TIMEOUT_MS
    ?? 55000,
)
const readinessTimeoutMs = Number(process.env.READINESS_TIMEOUT_MS ?? 3000)
const telemetryServiceUrl = (process.env.TELEMETRY_SERVICE_URL?.trim() || '').replace(/\/+$/, '')
const aiServiceSharedSecrets = parseSharedSecrets(
  process.env.AI_SERVICE_SHARED_SECRETS,
  process.env.AI_SERVICE_SHARED_SECRET,
)
const aiServiceSharedSecret = aiServiceSharedSecrets[0] ?? ''
const __dirname = path.dirname(fileURLToPath(import.meta.url))

// Resolved from this file, not the working directory, and pointed at the
// fleet's own manuals. The previous default was the doc-mcp prototype fixture
// directory, which holds none of the eight machines: a gateway started outside
// Compose answered 404 for every manual the manifest names.
const manualsDir = path.resolve(
  process.env.MANUALS_DIR ?? path.resolve(__dirname, '..', '..', 'requirements', 'manuals'),
)
const authConfig = buildAuthConfig()
const hstsEnabled = ['1', 'true', 'yes', 'on'].includes(String(process.env.GATEWAY_HSTS ?? '').toLowerCase())
const manualFrameAncestors = buildManualFrameAncestors(
  process.env.MANUAL_FRAME_ANCESTORS,
  corsOrigin,
)
const rateLimitWindowMs = Number(process.env.RATE_LIMIT_WINDOW_MS ?? 60000)
const rateLimitMax = Number(process.env.RATE_LIMIT_MAX ?? 120)
const rateLimiter = createRateLimiter({
  max: rateLimitMax,
  windowMs: rateLimitWindowMs,
})

// HttpError now lives in its own module so the access layer can raise the same
// typed errors the router already understands.

function setCommonHeaders(res, contentType = 'application/json') {
  res.setHeader('Access-Control-Allow-Origin', corsOrigin)
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PATCH,OPTIONS')
  res.setHeader(
    'Access-Control-Allow-Headers',
    'Content-Type,X-Request-Id,X-CSRF-Token,Idempotency-Key',
  )
  res.setHeader(
    'Access-Control-Expose-Headers',
    'X-Request-Id,Idempotency-Key,Idempotency-Replayed',
  )
  res.setHeader('Vary', 'Origin')
  if (corsOrigin !== '*') {
    res.setHeader('Access-Control-Allow-Credentials', 'true')
  }
  setSecurityHeaders(res)
  res.setHeader('Cache-Control', 'no-store')
  res.setHeader('Content-Type', contentType)
}

function setManualSecurityHeaders(res) {
  setSecurityHeaders(res)
  // X-Frame-Options cannot express an explicit cross-origin development
  // frontend. CSP frame-ancestors is the modern, allowlist-capable control.
  res.removeHeader('X-Frame-Options')
  res.setHeader(
    'Content-Security-Policy',
    `default-src 'none'; frame-ancestors ${manualFrameAncestors.join(' ')}; base-uri 'none'; form-action 'none'`,
  )
}

function setSecurityHeaders(res) {
  res.setHeader('X-Content-Type-Options', 'nosniff')
  res.setHeader('X-Frame-Options', 'DENY')
  res.setHeader('Referrer-Policy', 'no-referrer')
  res.setHeader('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
  res.setHeader(
    'Content-Security-Policy',
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
  )
  if (hstsEnabled) {
    res.setHeader('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
  }
}

/**
 * The one line a refusal is allowed to say over the wire.
 *
 * Clients branch on `code`; this is only what a curl or a log line reads. It
 * never repeats the diagnosis, so no response body can carry a token field, a
 * cookie name or an internal address to whoever asked.
 */
function refusalText(statusCode, error) {
  if (statusCode === 500) {
    return 'Internal gateway error.'
  }
  if (error.code) {
    return `Request refused (${error.code}).`
  }
  return error.message
}

function sendJson(res, statusCode, payload) {
  setCommonHeaders(res)
  res.writeHead(statusCode)
  res.end(JSON.stringify(payload))
}

function sendText(res, statusCode, payload, contentType = 'text/plain; version=0.0.4') {
  setCommonHeaders(res, contentType)
  res.writeHead(statusCode)
  res.end(payload)
}

async function enforceRateLimit(req) {
  await rateLimiter.enforce(rateLimitKey(req))
}

function rateLimitKey(req) {
  return authRateLimitKey(req)
}

async function authorizeRequest(req, url) {
  if (req.method === 'GET' && url.pathname === '/api/v1/auth/session') {
    try {
      return await authenticateRequest(req, authConfig)
    } catch (error) {
      if (error?.statusCode === 401) {
        return null
      }
      throw error
    }
  }

  if (isPublicPath(req.method, url.pathname)) {
    return null
  }

  return authenticateRequest(req, authConfig)
}

/**
 * Authorize a request for one machine's data.
 *
 * Runs both checks of the access model in order: the machine must belong to the
 * caller's company, then the caller's visibility must include the domain being
 * read. The deployment machine allowlist is still applied on top, so an
 * operator token can be narrowed further than its company.
 *
 * Returns the machine record, since the caller almost always needs it and it
 * has already been read to check tenancy.
 */
async function authorizeMachineAccess(req, machineId, domain = DOMAIN.COMMON) {
  const machine = await getMachineRecord(machineId)
  assertTenancy(req.authContext, machine)
  assertVisibility(req.authContext, domain)
  authorizeMachine(req.authContext, machine.machineId)
  return machine
}

function authorizeSupportAccess(req) {
  // Auth mode "off" is an explicit local-development escape hatch. Every
  // authenticated mode remains fail-closed unless a configured privileged
  // role is present.
  if (req.authContext?.mode === 'off') {
    return
  }

  const roles = req.authContext?.roles ?? []
  if (roles.some((role) => authConfig.adminRoles.includes(role))) {
    return
  }

  throw new HttpError(403, 'Support or administrator role is required.')
}

/**
 * Authorize a manual PDF.
 *
 * Manuals are machine identity and documentation, which every user of the
 * owning company may read whatever their visibility. Only the tenant boundary
 * applies, plus any deployment machine allowlist.
 */
async function authorizeManualAccess(req, fileName) {
  const machineIds = await getManualAccessMachineIds(fileName)
  if (!machineIds) {
    throw new HttpError(404, 'Manual file not found.', { code: 'manual_not_found' })
  }

  const machines = (await Promise.all(machineIds.map((id) => getMachineRecord(id)))).filter(Boolean)
  const inCompany = machines.filter((machine) => canSeeMachine(req.authContext, machine))

  if (inCompany.length === 0) {
    throw new HttpError(
      403,
      'This manual documents a machine that is not in your fleet.',
      { code: 'machine_not_in_company' },
    )
  }

  if (
    !req.authContext?.hasMachineRestriction
    || inCompany.some((machine) => req.authContext.machineIds.has(machine.machineId))
  ) {
    return
  }

  throw new HttpError(403, 'Manual access is not allowed for this identity.', {
    code: 'machine_not_allowed',
  })
}

function filterManualIndexStatus(req, status) {
  if (!req.authContext?.hasMachineRestriction || !Array.isArray(status.manuals)) {
    return status
  }

  const manuals = status.manuals.filter((manual) => req.authContext.machineIds.has(manual.machineId))
  const totalIndexedChunks = manuals.reduce(
    (total, manual) => total + (Number.isInteger(manual.indexedChunkCount) ? manual.indexedChunkCount : 0),
    0,
  )

  return {
    ...status,
    manuals,
    totalIndexedChunks,
    status: status.status === 'unavailable' ? status.status : totalIndexedChunks > 0 ? 'indexed' : 'empty',
  }
}

function filterMachines(req, machines) {
  if (!req.authContext?.hasMachineRestriction) {
    return machines
  }

  return machines.filter((machine) => req.authContext.machineIds.has(machine.id))
}

/**
 * The fleet this identity may see.
 *
 * Scoped to the caller's company at the query, not filtered afterwards, so a
 * machine belonging to another company is never read into memory in the first
 * place. A company that owns no machines gets an empty fleet, which is a true
 * answer rather than a denial.
 */
async function visibleMachines(req, requestId) {
  const companyId = tenantCompanyId(req.authContext)
  const includeTelemetry = canSeeDomain(req.authContext, DOMAIN.OPERATIONAL)
  return filterMachines(req, await getMachines(requestId, companyId, { includeTelemetry }))
}

/**
 * Resolve the dataset user a demo sign-in is for.
 *
 * There is no password: the demo is about demonstrating authorization, and the
 * dataset states every account is active. The user's own `companyId` and
 * `visibility` become token claims, so from here on the request is governed by
 * the same access model a real identity provider would drive.
 *
 * With no user supplied, the configured fallback identity is used, which keeps
 * existing scripted flows and the machine allowlist working.
 */
async function resolveDemoIdentity(body) {
  // With no user chosen, fall back to the configured default identity rather
  // than a synthetic subject: an identity with no company cannot pass the
  // tenant check, and silently exempting it would defeat the whole model.
  const requested = body?.userId ?? body?.email ?? authConfig.demoUserId
  if (!requested) {
    return null
  }

  if (typeof requested !== 'string' || requested.length > 320) {
    throw new HttpError(400, 'userId must be a string.', { code: 'invalid_user' })
  }

  const user = await findUser(requested)
  if (!user) {
    throw new HttpError(404, 'No such user in the fleet dataset.', { code: 'user_not_found' })
  }

  if (!isKnownVisibility(user.visibility)) {
    throw new HttpError(500, 'User has an unrecognized visibility level.', {
      code: 'visibility_unknown',
    })
  }

  // The machine claim is scoped to the user's own company, so the deployment
  // allowlist and the tenant boundary agree rather than fighting each other.
  const machines = await listMachines(user.companyId)

  return {
    subject: user.userId,
    userId: user.userId,
    companyId: user.companyId,
    visibility: user.visibility,
    roles: authConfig.demoRoles,
    machineIds: machines.map((machine) => machine.machineId),
  }
}

function authSessionPayload(req) {
  const authContext = req.authContext
  const cookieAuthToken = getCookieAuthToken(req, authConfig)

  const payload = {
    mode: authContext?.mode ?? authConfig.mode,
    subject: authContext?.subject ?? 'anonymous',
    roles: authContext?.roles ?? [],
    machineIds: authContext?.hasMachineRestriction ? [...authContext.machineIds] : [],
    hasMachineRestriction: Boolean(authContext?.hasMachineRestriction),
    authenticated: authContext?.mode !== 'off' && Boolean(authContext?.subject),
    // Identity metadata only. The UI uses this to show who is signed in and to
    // label what they cannot reach; every actual decision is made server-side.
    userId: authContext?.userId ?? null,
    companyId: authContext?.companyId ?? null,
    visibility: authContext?.visibility ?? null,
    domains: authContext?.visibility ? domainsFor(authContext.visibility) : [],
  }

  if (cookieAuthToken) {
    payload.csrfToken = csrfTokenForAuthToken(cookieAuthToken, authConfig)
  }

  return payload
}

function isPublicPath(method, pathname) {
  return (
    method === 'OPTIONS'
    || (method === 'GET' && pathname === '/health')
    || (method === 'GET' && pathname === '/ready')
    || (method === 'GET' && pathname === '/metrics')
    || (method === 'GET' && pathname === '/api/v1/auth/login')
    || (method === 'GET' && pathname === '/api/v1/auth/callback')
    || (method === 'GET' && pathname === '/api/v1/auth/logout/redirect')
    || (method === 'GET' && pathname === '/api/v1/auth/post-logout-callback')
    || (method === 'GET' && pathname === '/api/v1/auth/session')
    || (method === 'GET' && pathname === '/api/v1/auth/demo/users')
    || (method === 'POST' && pathname === '/api/v1/auth/demo/login')
    || (method === 'POST' && pathname === '/api/v1/auth/logout')
  )
}

function demoAuthPayload(req, token) {
  const authContext = req.authContext

  return {
    ...authSessionPayload(req),
    csrfToken: csrfTokenForAuthToken(token, authConfig),
    expiresInSeconds: authConfig.demoTokenTtlSeconds,
    authenticated: Boolean(authContext?.subject),
  }
}

function setAuthCookie(res, token, maxAge = authCookieMaxAge()) {
  const attributes = [
    `${authConfig.authCookieName}=${encodeURIComponent(token)}`,
    'Path=/',
    'HttpOnly',
    'SameSite=Lax',
    `Max-Age=${maxAge}`,
  ]

  if (authConfig.authCookieSecure) {
    attributes.push('Secure')
  }

  appendSetCookie(res, attributes.join('; '))
}

function clearAuthCookie(res) {
  setExpiredCookie(res, authConfig.authCookieName)
}

function setOidcStateCookie(res, state, maxAge) {
  setCookie(res, authConfig.oidcStateCookieName, state, maxAge)
}

function clearOidcStateCookie(res) {
  setExpiredCookie(res, authConfig.oidcStateCookieName)
}

function setOidcLogoutStateCookie(res, state, maxAge) {
  setCookie(res, authConfig.oidcLogoutStateCookieName, state, maxAge)
}

function clearOidcLogoutStateCookie(res) {
  setExpiredCookie(res, authConfig.oidcLogoutStateCookieName)
}

function setCookie(res, name, value, maxAge) {
  const attributes = [
    `${name}=${encodeURIComponent(value)}`,
    'Path=/',
    'HttpOnly',
    'SameSite=Lax',
    `Max-Age=${maxAge}`,
  ]

  if (authConfig.authCookieSecure) {
    attributes.push('Secure')
  }

  appendSetCookie(res, attributes.join('; '))
}

function appendSetCookie(res, value) {
  const current = res.getHeader('Set-Cookie')
  const cookies = current
    ? Array.isArray(current)
      ? current
      : [current]
    : []
  res.setHeader('Set-Cookie', [...cookies, value])
}

function setExpiredCookie(res, name) {
  const attributes = [
    `${name}=`,
    'Path=/',
    'HttpOnly',
    'SameSite=Lax',
    'Max-Age=0',
  ]

  if (authConfig.authCookieSecure) {
    attributes.push('Secure')
  }

  appendSetCookie(res, attributes.join('; '))
}

function authCookieMaxAge() {
  return authConfig.mode === 'demo'
    ? authConfig.demoTokenTtlSeconds
    : authConfig.authSessionTtlSeconds
}

function redirect(res, location) {
  setSecurityHeaders(res)
  res.setHeader('Cache-Control', 'no-store')
  res.setHeader('Referrer-Policy', 'no-referrer')
  res.writeHead(302, { Location: location })
  res.end()
}

function validReturnTo(value) {
  if (!value || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) {
    return '/'
  }

  try {
    const parsed = new URL(value, 'http://arol-q2.local')
    return `${parsed.pathname}${parsed.search}${parsed.hash}`
  } catch {
    return '/'
  }
}

function requestUrl(req) {
  try {
    // Parse the request target against a fixed origin. The Host header is
    // untrusted input and must never influence routing, metrics, or logging.
    return new URL(req.url ?? '/', 'http://arol-q2.local')
  } catch {
    throw new HttpError(400, 'Request target is invalid.')
  }
}

function requestPath(req) {
  try {
    return requestUrl(req).pathname
  } catch {
    return '/_invalid-request-target'
  }
}

async function sendManualFile(req, res, fileName) {
  await authorizeManualAccess(req, fileName)
  let filePath
  let fileStats

  try {
    filePath = resolveManualPath(fileName)
    fileStats = await stat(filePath)
  } catch (error) {
    if (error?.code === 'ENOENT') {
      throw new HttpError(404, 'Manual file not found.')
    }

    throw error
  }

  if (!fileStats.isFile()) {
    throw new HttpError(404, 'Manual file not found.')
  }

  const etag = `"${fileStats.size.toString(16)}-${Math.trunc(fileStats.mtimeMs).toString(16)}"`
  const lastModified = fileStats.mtime.toUTCString()
  setCommonHeaders(res, 'application/pdf')
  setManualSecurityHeaders(res)
  res.setHeader('Cache-Control', 'private, max-age=300')
  res.setHeader('Accept-Ranges', 'bytes')
  res.setHeader('ETag', etag)
  res.setHeader('Last-Modified', lastModified)
  res.setHeader('Content-Disposition', `inline; filename="${safeContentDispositionFileName(fileName)}"`)

  if (
    req.headers['if-none-match'] === etag
    || (!req.headers['if-none-match'] && isNotModifiedSince(req.headers['if-modified-since'], fileStats.mtimeMs))
  ) {
    res.writeHead(304)
    res.end()
    return
  }

  const ifRange = req.headers['if-range']
  const mayUseRange =
    !ifRange
    || ifRange === etag
    || isNotModifiedSince(ifRange, fileStats.mtimeMs)
  const range = mayUseRange ? parseByteRange(req.headers.range, fileStats.size) : null

  if (range?.invalid) {
    res.setHeader('Content-Range', `bytes */${fileStats.size}`)
    res.writeHead(416)
    res.end()
    return
  }

  const start = range?.start ?? 0
  const end = range?.end ?? fileStats.size - 1
  const contentLength = Math.max(0, end - start + 1)
  res.setHeader('Content-Length', String(contentLength))

  if (range) {
    res.setHeader('Content-Range', `bytes ${start}-${end}/${fileStats.size}`)
    res.writeHead(206)
  } else {
    res.writeHead(200)
  }

  if (req.method === 'HEAD' || contentLength === 0) {
    res.end()
    return
  }

  await pipeline(createReadStream(filePath, { start, end }), res)
}

function parseByteRange(value, size) {
  if (!value) {
    return null
  }

  if (typeof value !== 'string' || !value.startsWith('bytes=') || value.includes(',')) {
    return { invalid: true }
  }

  const match = value.slice(6).match(/^(\d*)-(\d*)$/)
  if (!match || (!match[1] && !match[2]) || size <= 0) {
    return { invalid: true }
  }

  if (!match[1]) {
    const suffixLength = Number(match[2])
    if (!Number.isSafeInteger(suffixLength) || suffixLength <= 0) {
      return { invalid: true }
    }
    return {
      start: Math.max(0, size - suffixLength),
      end: size - 1,
    }
  }

  const start = Number(match[1])
  const requestedEnd = match[2] ? Number(match[2]) : size - 1
  if (
    !Number.isSafeInteger(start)
    || !Number.isSafeInteger(requestedEnd)
    || start < 0
    || requestedEnd < start
    || start >= size
  ) {
    return { invalid: true }
  }

  return {
    start,
    end: Math.min(requestedEnd, size - 1),
  }
}

function isNotModifiedSince(value, modifiedAtMs) {
  if (typeof value !== 'string' || !value) {
    return false
  }

  const valueMs = Date.parse(value)
  return Number.isFinite(valueMs) && Math.trunc(modifiedAtMs / 1000) <= Math.trunc(valueMs / 1000)
}

async function parseJsonBody(req, byteLimit = maxBodyBytes) {
  const chunks = []
  let byteLength = 0

  for await (const chunk of req) {
    byteLength += chunk.length

    if (byteLength > byteLimit) {
      throw new HttpError(413, 'Request body is too large.')
    }

    chunks.push(chunk)
  }

  const raw = Buffer.concat(chunks).toString('utf8')

  try {
    return raw ? JSON.parse(raw) : {}
  } catch {
    throw new HttpError(400, 'Request body must be valid JSON.')
  }
}

function validateChatRequest(body) {
  if (!body || typeof body !== 'object') {
    return 'Request body must be a JSON object.'
  }

  if (typeof body.sessionId !== 'string' || body.sessionId.length === 0) {
    return 'sessionId is required.'
  }

  if (typeof body.machineId !== 'string' || body.machineId.length === 0) {
    return 'machineId is required.'
  }

  if (typeof body.message !== 'string' || body.message.trim().length === 0) {
    return 'message is required.'
  }

  if (body.message.length > 2000) {
    return 'message must be 2000 characters or fewer.'
  }

  const attachmentError = validateAttachments(body.attachments)
  if (attachmentError) {
    return attachmentError
  }

  return null
}

function validateAttachments(attachments) {
  if (attachments === undefined) {
    return null
  }

  if (!Array.isArray(attachments)) {
    return 'attachments must be an array when provided.'
  }

  if (attachments.length > maxAttachments) {
    return `attachments must contain at most ${maxAttachments} files.`
  }

  let totalBytes = 0
  for (let index = 0; index < attachments.length; index += 1) {
    const attachment = attachments[index]
    const label = `attachments[${index}]`

    if (!attachment || typeof attachment !== 'object' || Array.isArray(attachment)) {
      return `${label} must be an object.`
    }

    if (
      typeof attachment.name !== 'string'
      || attachment.name.length < 1
      || attachment.name.length > 160
      || /[\/\\\u0000-\u001f\u007f]/.test(attachment.name)
    ) {
      return `${label}.name must be a safe filename between 1 and 160 characters.`
    }

    if (!allowedAttachmentTypes.has(attachment.type)) {
      return `${label}.type is not supported.`
    }

    if (
      attachment.id !== undefined
      && (
        typeof attachment.id !== 'string'
        || attachment.id.length < 1
        || attachment.id.length > 128
        || !/^[A-Za-z0-9._:-]+$/.test(attachment.id)
      )
    ) {
      return `${label}.id must contain 1 to 128 safe characters.`
    }

    const decoded = decodeBase64(attachment.contentBase64)
    if (!decoded) {
      return `${label}.contentBase64 must be non-empty canonical base64.`
    }

    if (decoded.length > maxAttachmentBytes) {
      return `${label} exceeds the ${maxAttachmentBytes}-byte file limit.`
    }

    if (!attachmentContentMatchesType(attachment.type, decoded)) {
      return `${label}.contentBase64 does not match the declared type.`
    }

    if (
      attachment.size !== undefined
      && (!Number.isInteger(attachment.size) || attachment.size !== decoded.length)
    ) {
      return `${label}.size must match the decoded content length.`
    }

    totalBytes += decoded.length
    if (totalBytes > maxTotalAttachmentBytes) {
      return `attachments exceed the ${maxTotalAttachmentBytes}-byte total limit.`
    }
  }

  return null
}

function normalizeChatRequest(req, body) {
  const idempotencyKey = readIdempotencyKey(req, body)
  const attachments = (body.attachments ?? []).map((attachment) => {
    const content = Buffer.from(attachment.contentBase64, 'base64')
    return {
      id: attachment.id || attachmentId(attachment, content),
      name: attachment.name,
      type: attachment.type,
      size: content.length,
      contentBase64: attachment.contentBase64,
    }
  })

  req.idempotencyKey = idempotencyKey
  return {
    ...body,
    ...(idempotencyKey ? { idempotencyKey } : {}),
    ...(body.attachments !== undefined ? { attachments } : {}),
  }
}

function readIdempotencyKey(req, body) {
  const headerValue = req.headers['idempotency-key']
  const headerKey = Array.isArray(headerValue) ? headerValue[0] : headerValue
  const bodyKey = body?.idempotencyKey

  if (bodyKey !== undefined && typeof bodyKey !== 'string') {
    throw new HttpError(400, 'idempotencyKey must be a string when provided.')
  }

  if (headerKey && bodyKey && headerKey !== bodyKey) {
    throw new HttpError(400, 'Idempotency-Key header and body idempotencyKey must match.')
  }

  const key = headerKey ?? bodyKey
  if (
    key !== undefined
    && (key.length < 1 || key.length > 128 || !/^[A-Za-z0-9._:-]+$/.test(key))
  ) {
    throw new HttpError(
      400,
      'Idempotency-Key must contain 1 to 128 letters, digits, dots, underscores, colons, or hyphens.',
    )
  }

  return key
}

function exposeIdempotencyKey(res, req) {
  if (req.idempotencyKey) {
    res.setHeader('Idempotency-Key', req.idempotencyKey)
  }
}

function attachmentContentMatchesType(type, content) {
  if (type === 'application/pdf') {
    return content.length >= 5 && content.subarray(0, 5).toString('ascii') === '%PDF-'
  }

  if (type === 'text/plain') {
    if (content.includes(0)) {
      return false
    }

    try {
      new TextDecoder('utf-8', { fatal: true }).decode(content)
      return true
    } catch {
      return false
    }
  }

  return false
}

function decodeBase64(value) {
  if (
    typeof value !== 'string'
    || value.length === 0
    || value.length % 4 !== 0
    || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)
  ) {
    return null
  }

  const decoded = Buffer.from(value, 'base64')
  return decoded.toString('base64') === value ? decoded : null
}

function attachmentId(attachment, content) {
  return `att-${createHash('sha256')
    .update(attachment.name)
    .update('\0')
    .update(attachment.type)
    .update('\0')
    .update(content)
    .digest('hex')
    .slice(0, 24)}`
}

function validateManualSearchRequest(body) {
  if (!body || typeof body !== 'object') {
    return 'Request body must be a JSON object.'
  }

  if (typeof body.machineId !== 'string' || body.machineId.length === 0) {
    return 'machineId is required.'
  }

  if (typeof body.query !== 'string' || body.query.trim().length === 0) {
    return 'query is required.'
  }

  if (body.query.length > 2000) {
    return 'query must be 2000 characters or fewer.'
  }

  if (body.limit !== undefined && (!Number.isInteger(body.limit) || body.limit < 1 || body.limit > 10)) {
    return 'limit must be an integer between 1 and 10.'
  }

  return null
}

async function completeChat(body, req, requestId) {
  return postAiJson('/api/v1/chat/complete', body, aiServiceTimeoutMs, req, requestId)
}

async function getAiJson(path, req, requestId, timeoutMs = aiServiceTimeoutMs) {
  let response

  try {
    response = await fetch(`${aiServiceUrl}${path}`, {
      headers: aiHeaders(req, requestId),
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch (error) {
    throw upstreamUnavailable('assistant request', error)
  }

  if (!response.ok) {
    throw new HttpError(aiErrorStatus(response.status), await aiErrorMessage(response))
  }

  return response.json()
}

async function draftEscalation(body, req, requestId) {
  return postAiJson('/api/v1/escalations/draft', body, aiServiceTimeoutMs, req, requestId)
}

/**
 * A 502 for an unreachable AI service.
 *
 * The address and the transport error are logged, never returned: the browser
 * gets a code it can branch on and wording an operator can act on, because
 * "The operation was aborted due to timeout" at http://ai-service:8000 is
 * neither.
 */
function upstreamUnavailable(what, error) {
  console.warn(
    JSON.stringify({
      eventType: 'upstream.unavailable',
      upstream: aiServiceUrl,
      what,
      error: errorMessage(error),
    }),
  )
  return new HttpError(502, 'The assistant is temporarily unavailable.', {
    code: 'upstream_unavailable',
  })
}

async function postAiJson(path, body, timeoutMs, req, requestId) {
  let response

  try {
    response = await fetch(`${aiServiceUrl}${path}`, {
      method: 'POST',
      headers: aiHeaders(req, requestId, {
        'Content-Type': 'application/json',
      }),
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch (error) {
    throw upstreamUnavailable('assistant request', error)
  }

  if (!response.ok) {
    throw new HttpError(aiErrorStatus(response.status), await aiErrorMessage(response))
  }

  const payload = await response.json()
  if (payload && (typeof payload === 'object' || typeof payload === 'function')) {
    Object.defineProperty(payload, idempotencyReplayed, {
      value: response.headers.get('idempotency-replayed') === 'true',
      enumerable: false,
    })
  }
  return payload
}

async function completeAndPersistChat(body, requestId, req) {
  const response = await completeChat(body, req, requestId)
  await handleChatOperationalEvents({
    request: body,
    response,
    requestId,
    authContext: req.authContext,
    mode: 'complete',
  })

  return response
}

async function streamAiServiceChat(request, req, res) {
  const requestId = String(res.getHeader('X-Request-Id') ?? randomUUID())
  const upstreamStartedAt = Date.now()
  const controller = new AbortController()
  const connectTimer = setTimeout(
    () => controller.abort(new Error('AI service stream connect timeout.')),
    aiServiceStreamConnectTimeoutMs,
  )
  let response
  try {
    response = await fetch(`${aiServiceUrl}/api/v1/chat/stream`, {
      method: 'POST',
      headers: aiHeaders(req, requestId, {
        'Content-Type': 'application/json',
      }),
      body: JSON.stringify(request),
      signal: controller.signal,
    })
  } catch (error) {
    throw upstreamUnavailable('assistant stream', error)
  } finally {
    clearTimeout(connectTimer)
  }

  if (!response.ok) {
    throw new HttpError(aiErrorStatus(response.status), await aiErrorMessage(response))
  }

  if (!response.body) {
    throw new HttpError(502, 'AI service stream response did not include a body.')
  }

  const replayed = response.headers.get('idempotency-replayed') === 'true'
  setCommonHeaders(res, 'text/event-stream')
  res.setHeader('Cache-Control', 'no-cache, no-store')
  res.setHeader('Connection', 'keep-alive')
  res.setHeader('Idempotency-Replayed', String(replayed))
  res.writeHead(200)

  const decoder = new TextDecoder()
  let eventBuffer = ''
  let doneEventText = ''
  let finalResponse = null
  let firstTokenRecorded = false
  let idleTimer
  const processEventText = (text, terminated = false) => {
    eventBuffer += text
    const events = eventBuffer.split(/\r?\n\r?\n/)
    eventBuffer = events.pop() ?? ''
    if (terminated && eventBuffer.trim()) {
      events.push(eventBuffer)
      eventBuffer = ''
    }

    for (const event of events) {
      if (!event.trim()) {
        continue
      }

      const framedEvent = `${event}\n\n`
      if (/^event:\s*token\s*$/m.test(event) && !firstTokenRecorded) {
        recordStreamFirstTokenMetric(Date.now() - upstreamStartedAt)
        firstTokenRecorded = true
      }

      if (/^event:\s*done\s*$/m.test(event)) {
        doneEventText = framedEvent
        finalResponse = parseDoneEvent(event)
        continue
      }

      if (doneEventText) {
        throw new Error('AI service emitted data after the terminal done event.')
      }
      res.write(framedEvent)
    }
  }
  const armIdleTimeout = () => {
    clearTimeout(idleTimer)
    idleTimer = setTimeout(
      () => controller.abort(new Error('AI service stream idle timeout.')),
      aiServiceStreamIdleTimeoutMs,
    )
  }
  armIdleTimeout()

  try {
    for await (const chunk of response.body) {
      armIdleTimeout()
      if (res.destroyed || res.writableEnded) {
        controller.abort()
        return
      }

      const text = decoder.decode(chunk, { stream: true })
      processEventText(text)
    }
    processEventText(decoder.decode(), true)
  } catch (error) {
    if (!res.destroyed && !res.writableEnded) {
      res.write(
        `event: error\ndata: ${JSON.stringify({
          code: 'upstream_stream_interrupted',
          message: 'The AI response stream was interrupted.',
        })}\n\n`,
      )
      res.end()
    }
    return
  } finally {
    clearTimeout(idleTimer)
  }

  if (!doneEventText || !finalResponse?.message) {
    if (!res.destroyed && !res.writableEnded) {
      res.write(
        `event: error\ndata: ${JSON.stringify({
          code: 'invalid_upstream_stream',
          message: 'The AI response stream ended without a valid final response.',
        })}\n\n`,
      )
      res.end()
    }
    return
  }

  try {
    await handleChatOperationalEvents({
      request,
      response: finalResponse,
      requestId,
      authContext: req.authContext,
      mode: 'stream',
    })
  } catch (error) {
    recordOperationsWriteFailure('stream-finalization')
    console.error(
      JSON.stringify({
        eventType: 'operations.write-failed',
        kind: 'stream-finalization',
        requestId,
        error: error instanceof Error ? error.name : 'Error',
      }),
    )
    if (!res.destroyed && !res.writableEnded) {
      res.write(
        `event: error\ndata: ${JSON.stringify({
          code: 'operational_commit_failed',
          message: 'The response could not be finalized safely. Retry with the same request key.',
        })}\n\n`,
      )
      res.end()
    }
    return
  }

  res.write(doneEventText)
  res.end()
}

function wasIdempotencyReplayed(payload) {
  return Boolean(payload?.[idempotencyReplayed])
}

function exposeIdempotencyReplay(res, payload) {
  res.setHeader('Idempotency-Replayed', String(wasIdempotencyReplayed(payload)))
}

function parseDoneEvent(rawEvents) {
  const doneIndex = rawEvents.lastIndexOf('event: done')

  if (doneIndex === -1) {
    return null
  }

  const doneEvent = rawEvents.slice(doneIndex)
  const dataLine = doneEvent
    .split(/\r?\n/)
    .find((line) => line.startsWith('data: '))

  if (!dataLine) {
    return null
  }

  try {
    return JSON.parse(dataLine.slice('data: '.length))
  } catch {
    return null
  }
}

async function streamChatRequest(request, req, res) {
  const error = validateChatRequest(request)

  if (error) {
    sendJson(res, 400, { error })
    return
  }

  request = normalizeChatRequest(req, request)
  exposeIdempotencyKey(res, req)
  request.machineId = (
    await authorizeMachineAccess(req, request.machineId, DOMAIN.COMMON)
  ).machineId
  await streamAiServiceChat(request, req, res)
}

function aiHeaders(req, requestId, extra = {}) {
  const headers = {
    ...extra,
    'X-Request-Id': requestId,
    'X-Arol-Subject': req.authContext?.subject ?? 'anonymous',
    'X-Arol-Roles': (req.authContext?.roles ?? []).join(','),
    'X-Arol-Machine-Ids': req.authContext?.hasMachineRestriction
      ? [...req.authContext.machineIds].join(',')
      : '',
    // The access model, forwarded so the agents enforce it on their own tools
    // rather than trusting that the gateway already filtered everything.
    'X-Arol-User-Id': req.authContext?.userId ?? '',
    'X-Arol-Company-Id': req.authContext?.companyId ?? '',
    'X-Arol-Visibility': req.authContext?.visibility ?? '',
    'X-Arol-Is-Staff': req.authContext?.isAdmin ? 'true' : 'false',
    'X-Arol-Auth-Mode': req.authContext?.mode ?? authConfig.mode,
    ...(req.idempotencyKey ? { 'Idempotency-Key': req.idempotencyKey } : {}),
  }

  if (aiServiceSharedSecret) {
    headers['X-Arol-Internal-Secret'] = aiServiceSharedSecret
  }

  return headers
}

function parseSharedSecrets(rotatingValue, fallbackValue) {
  const source = rotatingValue?.trim() || fallbackValue?.trim() || ''
  return source.split(',').map((value) => value.trim()).filter(Boolean)
}

function buildManualFrameAncestors(configuredValue, fallbackOrigin) {
  const candidates = configuredValue
    ? configuredValue.split(/\s+/)
    : ["'self'", fallbackOrigin]
  const ancestors = candidates
    .map((value) => value.trim())
    .filter(Boolean)
    .filter((value) => value === "'self'" || isHttpOrigin(value))

  return [...new Set(ancestors.length > 0 ? ancestors : ["'self'"])]
}

function isHttpOrigin(value) {
  try {
    const parsed = new URL(value)
    return (
      ['http:', 'https:'].includes(parsed.protocol)
      && parsed.origin === value
      && !parsed.username
      && !parsed.password
    )
  } catch {
    return false
  }
}

function safeContentDispositionFileName(value) {
  return decodeURIComponent(value)
    .replace(/[^\x20-\x7e]/g, '_')
    .replace(/["\\]/g, '_')
}

async function gatewayReadiness(requestId) {
  const dependencies = {}

  const [ai, manuals, operations, telemetry] = await Promise.all([
    probeJsonDependency(`${aiServiceUrl}/ready`, requestId, true),
    probeManualDependency(),
    operationsReadiness(),
    telemetryServiceUrl
      ? probeJsonDependency(`${telemetryServiceUrl}/health`, requestId, false)
      : Promise.resolve({ status: 'disabled', required: false }),
  ])

  dependencies.aiService = ai
  dependencies.authentication = authConfigurationReadiness(authConfig)
  dependencies.manuals = manuals
  dependencies.operations = operations
  dependencies.telemetry = telemetry

  const unavailable = Object.entries(dependencies)
    .filter(([, dependency]) => dependency.required !== false && dependency.status !== 'ready')
    .map(([name]) => name)

  return {
    statusCode: unavailable.length > 0 ? 503 : 200,
    payload: {
      status: unavailable.length > 0 ? 'not_ready' : 'ready',
      service: 'gateway-service',
      dependencies,
      ...(unavailable.length > 0 ? { unavailable } : {}),
    },
  }
}

async function probeJsonDependency(url, requestId, includeInternalSecret) {
  try {
    const response = await fetch(url, {
      headers: {
        Accept: 'application/json',
        'X-Request-Id': requestId,
        ...(includeInternalSecret && aiServiceSharedSecret
          ? { 'X-Arol-Internal-Secret': aiServiceSharedSecret }
          : {}),
      },
      signal: AbortSignal.timeout(readinessTimeoutMs),
    })
    let payload = null
    try {
      payload = await response.json()
    } catch {
      // A dependency can still be reported without returning its invalid body.
    }

    return {
      status: response.ok ? 'ready' : 'unavailable',
      required: true,
      httpStatus: response.status,
      ...(payload?.dependencies ? { dependencies: payload.dependencies } : {}),
    }
  } catch (error) {
    return {
      status: 'unavailable',
      required: true,
      error: errorMessage(error),
    }
  }
}

async function probeManualDependency() {
  try {
    const manuals = await getManuals()
    const availability = await Promise.all(manuals.map(async (manual) => {
      try {
        const pathname = new URL(manual.url, 'http://arol-q2.local').pathname
        await access(resolveManualPath(path.basename(pathname)))
        return true
      } catch {
        return false
      }
    }))
    const missing = availability.filter((available) => !available).length
    return {
      status: manuals.length > 0 && missing === 0 ? 'ready' : 'unavailable',
      required: true,
      count: manuals.length,
      missing,
    }
  } catch (error) {
    return {
      status: 'unavailable',
      required: true,
      error: errorMessage(error),
    }
  }
}

function scopedContract(contract, req) {
  if (!contract) return null
  const result = { ...contract }
  if (!canSeeDomain(req.authContext, DOMAIN.OPERATIONAL)) {
    delete result.openTicketCount
    delete result.ticketCount
    delete result.lastScheduledMaintenance
  }
  return result
}

async function getOptionalBusinessContract(machineId, req, requestId) {
  try {
    machineId = (await getMachineRecord(machineId)).machineId
    const context = await getAiJson(
      `/api/v1/machines/${encodeURIComponent(machineId)}/context`,
      req,
      requestId,
    )
    return scopedContract(context.contract, req)
  } catch (error) {
    if ([404, 502, 503].includes(error?.statusCode)) {
      console.warn(JSON.stringify({
        eventType: 'business-context.unavailable',
        requestId,
        machineId,
        detail: errorMessage(error),
      }))
      return null
    }

    throw error
  }
}

function aiErrorStatus(statusCode) {
  return statusCode >= 400 && statusCode < 500 ? statusCode : 502
}

async function aiErrorMessage(response) {
  const fallback = `AI service returned ${response.status}.`

  try {
    const payload = await response.json()
    return payload.detail || payload.error || fallback
  } catch {
    return fallback
  }
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error)
}

function queryLimit(url, fallback = 100, max = 500) {
  const value = Number(url.searchParams.get('limit') ?? fallback)
  return Number.isInteger(value) && value > 0 ? Math.min(value, max) : fallback
}

function resolveManualPath(fileName) {
  let decodedFileName
  try {
    decodedFileName = decodeURIComponent(fileName)
  } catch {
    throw new HttpError(400, 'Manual path is invalid.')
  }
  const resolved = path.resolve(manualsDir, decodedFileName)
  const root = `${manualsDir}${path.sep}`

  if (!resolved.startsWith(root)) {
    throw new HttpError(400, 'Manual path is invalid.')
  }

  return resolved
}

async function route(req, res) {
  const url = requestUrl(req)

  if (req.method === 'OPTIONS') {
    setCommonHeaders(res)
    res.writeHead(204)
    res.end()
    return
  }

  if (req.method === 'GET' && url.pathname === '/health') {
    sendJson(res, 200, { status: 'ok', service: 'gateway-service' })
    return
  }

  if (req.method === 'GET' && url.pathname === '/ready') {
    const readiness = await gatewayReadiness(String(res.getHeader('X-Request-Id')))
    sendJson(res, readiness.statusCode, readiness.payload)
    return
  }

  req.authContext = await authorizeRequest(req, url)
  const isDemoLogin = req.method === 'POST' && url.pathname === '/api/v1/auth/demo/login'
  if (!isDemoLogin) {
    requireCsrfForCookieAuth(req, authConfig)
  }
  await enforceRateLimit(req)

  if (req.method === 'GET' && url.pathname === '/api/v1/auth/login') {
    const login = await beginOidcLogin(authConfig, validReturnTo(url.searchParams.get('returnTo')))
    setOidcStateCookie(res, login.state, login.maxAge)
    redirect(res, login.authorizationUrl)
    return
  }

  if (req.method === 'GET' && url.pathname === '/api/v1/auth/callback') {
    const state = url.searchParams.get('state') ?? ''
    const code = url.searchParams.get('code') ?? ''
    const stateCookie = getCookieValue(req, authConfig.oidcStateCookieName)
    clearOidcStateCookie(res)

    if (!state || !code || !stateCookie || stateCookie !== state) {
      sendJson(res, 400, { error: 'OIDC callback state or code is invalid.' })
      return
    }

    const login = await completeOidcLogin({ config: authConfig, state, code })
    setAuthCookie(res, login.session.sessionId)
    redirect(res, login.returnTo)
    return
  }

  if (req.method === 'GET' && url.pathname === '/api/v1/auth/logout/redirect') {
    const state = getCookieValue(req, authConfig.oidcLogoutStateCookieName)
    const providerLogoutUrl = await oidcLogoutRedirect({ config: authConfig, state })
    clearOidcLogoutStateCookie(res)

    if (providerLogoutUrl) {
      redirect(res, providerLogoutUrl)
      return
    }

    redirect(res, await completeOidcLogout(state ?? ''))
    return
  }

  if (req.method === 'GET' && url.pathname === '/api/v1/auth/post-logout-callback') {
    const returnTo = await completeOidcLogout(url.searchParams.get('state') ?? '')
    clearOidcLogoutStateCookie(res)
    redirect(res, validReturnTo(returnTo))
    return
  }

  if (req.authContext?.sessionId && !getBearerToken(req)) {
    // Keep an active browser session alive without exposing or rotating the
    // provider token in the browser. Refresh-token rotation happens server-side.
    setAuthCookie(res, req.authContext.sessionId)
  }

  // The identities the demo can sign in as: every user in the fleet dataset,
  // grouped by company so the access model is visible before anyone signs in.
  if (req.method === 'GET' && url.pathname === '/api/v1/auth/demo/users') {
    if (authConfig.mode !== 'demo') {
      sendJson(res, 404, { error: 'Demo auth is not enabled.' })
      return
    }

    const users = await listUsers()
    sendJson(res, 200, {
      users: users.map((user) => ({
        userId: user.userId,
        name: `${user.firstName} ${user.lastName}`,
        email: user.email,
        jobTitle: user.jobTitle,
        visibility: user.visibility,
        companyId: user.companyId,
        companyName: user.companyName,
        country: user.country,
        // What this identity will and will not be able to read.
        domains: domainsFor(user.visibility),
      })),
    })
    return
  }

  if (req.method === 'POST' && url.pathname === '/api/v1/auth/demo/login') {
    if (authConfig.mode !== 'demo') {
      sendJson(res, 404, { error: 'Demo auth is not enabled.' })
      return
    }

    const identity = await resolveDemoIdentity(await parseJsonBody(req))
    const token = createDemoJwt(authConfig, identity)
    setAuthCookie(res, token)
    req.authContext = await authenticateRequest(
      {
        ...req,
        headers: {
          ...req.headers,
          cookie: `${authConfig.authCookieName}=${encodeURIComponent(token)}`,
        },
      },
      authConfig,
    )
    sendJson(res, 200, demoAuthPayload(req, token))
    return
  }

  if (req.method === 'POST' && url.pathname === '/api/v1/auth/logout') {
    const body = await parseJsonBody(req)
    const sessionId = req.authContext?.sessionId ?? getCookieValue(req, authConfig.authCookieName)
    const providerLogout = await beginOidcLogout({
      config: authConfig,
      sessionId,
      returnTo: validReturnTo(body?.returnTo),
    })
    await logoutAuthSession(sessionId)
    clearAuthCookie(res)
    clearOidcStateCookie(res)
    clearOidcLogoutStateCookie(res)
    if (providerLogout) {
      setOidcLogoutStateCookie(res, providerLogout.state, providerLogout.maxAge)
    }
    sendJson(res, 200, {
      mode: authConfig.mode,
      subject: 'anonymous',
      roles: [],
      machineIds: [],
      hasMachineRestriction: false,
      authenticated: false,
      logoutRequired: Boolean(providerLogout),
    })
    return
  }

  if (req.method === 'POST' && url.pathname === '/api/v1/auth/demo/logout') {
    if (authConfig.mode !== 'demo') {
      sendJson(res, 404, { error: 'Demo auth is not enabled.' })
      return
    }

    clearAuthCookie(res)
    sendJson(res, 200, {
      mode: 'demo',
      subject: 'anonymous',
      roles: [],
      machineIds: [],
      hasMachineRestriction: true,
      authenticated: false,
    })
    return
  }

  if (req.method === 'GET' && url.pathname === '/metrics') {
    sendText(res, 200, await prometheusMetrics())
    return
  }

  if (req.method === 'GET' && url.pathname === '/api/v1/auth/session') {
    sendJson(res, 200, authSessionPayload(req))
    return
  }

  const manualFileMatch = url.pathname.match(/^\/manuals\/([^/]+\.pdf)$/)
  if ((req.method === 'GET' || req.method === 'HEAD') && manualFileMatch) {
    await sendManualFile(req, res, manualFileMatch[1])
    return
  }

  if (req.method === 'GET' && url.pathname === '/api/v1/machines') {
    sendJson(res, 200, {
      machines: await visibleMachines(req, String(res.getHeader('X-Request-Id'))),
    })
    return
  }

  if (req.method === 'GET' && url.pathname === '/api/v1/manuals/index/status') {
    const status = await getAiJson('/api/v1/manuals/index/status', req, String(res.getHeader('X-Request-Id')))
    sendJson(res, 200, filterManualIndexStatus(req, status))
    return
  }

  if (req.method === 'POST' && url.pathname === '/api/v1/manuals/search') {
    const body = await parseJsonBody(req)
    const error = validateManualSearchRequest(body)

    if (error) {
      sendJson(res, 400, { error })
      return
    }

    // A QR code may carry the serial number, which the README allows. The
    // AI service knows machines by machineId alone, so forward the resolved
    // identity rather than whatever the code happened to encode.
    body.machineId = (await authorizeMachineAccess(req, body.machineId, DOMAIN.COMMON)).machineId
    sendJson(
      res,
      200,
      await postAiJson(
        '/api/v1/manuals/search',
        body,
        aiServiceTimeoutMs,
        req,
        String(res.getHeader('X-Request-Id')),
      ),
    )
    return
  }

  const machineMatch = url.pathname.match(/^\/api\/v1\/machines\/([^/]+)$/)
  if (req.method === 'GET' && machineMatch) {
    await authorizeMachineAccess(req, machineMatch[1], DOMAIN.COMMON)
    const machine = await getMachine(machineMatch[1], String(res.getHeader('X-Request-Id')), {
      includeTelemetry: canSeeDomain(req.authContext, DOMAIN.OPERATIONAL),
    })
    sendJson(res, machine ? 200 : 404, machine ?? { error: 'Machine not found.' })
    return
  }

  const contextMatch = url.pathname.match(/^\/api\/v1\/machines\/([^/]+)\/context$/)
  if (req.method === 'GET' && contextMatch) {
    await authorizeMachineAccess(req, contextMatch[1], DOMAIN.COMMON)
    const requestId = String(res.getHeader('X-Request-Id'))

    // Machine identity and documentation are common to every user of the owning
    // company, but this one payload also bundles operational and commercial
    // data. Each part is included only if the caller's visibility covers it, and
    // the parts that are withheld say so rather than arriving as null, which
    // would read as "this machine has no telemetry".
    const seesOperational = canSeeDomain(req.authContext, DOMAIN.OPERATIONAL)
    const seesCommercial = canSeeDomain(req.authContext, DOMAIN.COMMERCIAL)

    const [context, contract] = await Promise.all([
      getMachineContext(contextMatch[1], requestId, { includeTelemetry: seesOperational }),
      seesCommercial ? getOptionalBusinessContract(contextMatch[1], req, requestId) : null,
    ])

    if (!context.machine) {
      sendJson(res, 404, { error: 'Machine not found.' })
      return
    }

    sendJson(res, 200, {
      ...context,
      contract,
      withheld: [
        ...(seesOperational ? [] : [{ domain: DOMAIN.OPERATIONAL, reason: 'visibility_denied' }]),
        ...(seesCommercial ? [] : [{ domain: DOMAIN.COMMERCIAL, reason: 'visibility_denied' }]),
      ],
    })
    return
  }

  const documentMatch = url.pathname.match(/^\/api\/v1\/machines\/([^/]+)\/documents$/)
  if (req.method === 'GET' && documentMatch) {
    await authorizeMachineAccess(req, documentMatch[1], DOMAIN.COMMON)
    const machine = await getMachine(documentMatch[1])

    if (!machine) {
      sendJson(res, 404, { error: 'Machine not found.' })
      return
    }

    // The machine's own use-and-maintenance manual is the document set the
    // dataset provides. This endpoint used to be permanently empty.
    const manual = await getManual(machine.id)
    sendJson(
      res,
      200,
      manual
        ? [
            {
              type: 'manual',
              title: manual.title,
              url: manual.url,
              language: manual.language,
              serialNumber: manual.serialNumber,
            },
            ...manual.relatedManuals.map((related) => ({
              type: 'related-manual',
              title: related.title,
              url: related.url,
              language: related.language,
              serialNumber: related.serialNumber,
            })),
          ]
        : [],
    )
    return
  }

  const telemetryMatch = url.pathname.match(/^\/api\/v1\/machines\/([^/]+)\/telemetry\/latest$/)
  if (req.method === 'GET' && telemetryMatch) {
    await authorizeMachineAccess(req, telemetryMatch[1], DOMAIN.OPERATIONAL)
    const telemetry = await getLatestTelemetry(
      telemetryMatch[1],
      String(res.getHeader('X-Request-Id')),
    )
    sendJson(res, telemetry ? 200 : 404, telemetry ?? { error: 'Telemetry not found.' })
    return
  }

  const telemetryHistoryMatch = url.pathname.match(/^\/api\/v1\/machines\/([^/]+)\/telemetry\/history$/)
  if (req.method === 'GET' && telemetryHistoryMatch) {
    await authorizeMachineAccess(req, telemetryHistoryMatch[1], DOMAIN.OPERATIONAL)
    const requestId = String(res.getHeader('X-Request-Id'))
    const machine = await getMachine(telemetryHistoryMatch[1], requestId)

    if (!machine) {
      sendJson(res, 404, { error: 'Machine not found.' })
      return
    }

    const limit = Number(url.searchParams.get('limit') ?? 24)
    sendJson(res, 200, await getMachineTelemetryHistory(telemetryHistoryMatch[1], limit, requestId))
    return
  }

  const alarmsMatch = url.pathname.match(/^\/api\/v1\/machines\/([^/]+)\/alarms$/)
  if (req.method === 'GET' && alarmsMatch) {
    await authorizeMachineAccess(req, alarmsMatch[1], DOMAIN.OPERATIONAL)
    const requestId = String(res.getHeader('X-Request-Id'))
    const machine = await getMachine(alarmsMatch[1], requestId)

    if (!machine) {
      sendJson(res, 404, { error: 'Machine not found.' })
      return
    }

    sendJson(res, 200, await getMachineAlarms(alarmsMatch[1], requestId))
    return
  }

  const contractMatch = url.pathname.match(/^\/api\/v1\/machines\/([^/]+)\/contract$/)
  if (req.method === 'GET' && contractMatch) {
    const machine = await authorizeMachineAccess(req, contractMatch[1], DOMAIN.COMMERCIAL)
    const contract = await getAiJson(
      `/api/v1/machines/${encodeURIComponent(machine.machineId)}/contract`,
      req,
      String(res.getHeader('X-Request-Id')),
    )
    sendJson(res, 200, scopedContract(contract, req))
    return
  }

  const warrantyMatch = url.pathname.match(/^\/api\/v1\/machines\/([^/]+)\/warranty$/)
  if (req.method === 'GET' && warrantyMatch) {
    const machine = await authorizeMachineAccess(req, warrantyMatch[1], DOMAIN.COMMERCIAL)
    const warranty = await getAiJson(
      `/api/v1/machines/${encodeURIComponent(machine.machineId)}/warranty`,
      req,
      String(res.getHeader('X-Request-Id')),
    )
    sendJson(res, 200, scopedContract(warranty, req))
    return
  }

  const ordersMatch = url.pathname.match(/^\/api\/v1\/machines\/([^/]+)\/orders$/)
  if (req.method === 'GET' && ordersMatch) {
    const machine = await authorizeMachineAccess(req, ordersMatch[1], DOMAIN.COMMERCIAL)
    sendJson(res, 200, await listOrders(machine.companyId, machine.machineId))
    return
  }

  // Quotations issued to the caller's own company. A quote has no status of its
  // own: each entry carries its current revision, which is the highest-numbered
  // one, and line prices are already net of that revision's discount.
  if (req.method === 'GET' && url.pathname === '/api/v1/quotes') {
    assertVisibility(req.authContext, DOMAIN.COMMERCIAL)
    const companyId = tenantCompanyId(req.authContext)
    if (!companyId) {
      sendJson(res, 200, { quotes: [] })
      return
    }
    sendJson(res, 200, { quotes: await listQuotes(companyId) })
    return
  }

  // Maintenance tickets are operational data, not commercial: they describe how
  // the machine has been serviced.
  const ticketsMatch = url.pathname.match(/^\/api\/v1\/machines\/([^/]+)\/maintenance$/)
  if (req.method === 'GET' && ticketsMatch) {
    const machine = await authorizeMachineAccess(req, ticketsMatch[1], DOMAIN.OPERATIONAL)
    sendJson(res, 200, { tickets: await listMaintenanceTickets(machine.machineId) })
    return
  }

  const serviceHistoryMatch = url.pathname.match(/^\/api\/v1\/machines\/([^/]+)\/service-history$/)
  if (req.method === 'GET' && serviceHistoryMatch) {
    const machine = await authorizeMachineAccess(req, serviceHistoryMatch[1], DOMAIN.OPERATIONAL)
    const history = await getAiJson(
      `/api/v1/machines/${encodeURIComponent(machine.machineId)}/service-history`,
      req,
      String(res.getHeader('X-Request-Id')),
    )
    sendJson(res, 200, history)
    return
  }

  const manualMatch = url.pathname.match(/^\/api\/v1\/manuals\/([^/]+)$/)
  if (req.method === 'GET' && manualMatch) {
    await authorizeMachineAccess(req, manualMatch[1], DOMAIN.COMMON)
    const manual = await getManual(manualMatch[1])
    sendJson(res, manual ? 200 : 404, manual ?? { error: 'Manual not found.' })
    return
  }

  if (req.method === 'POST' && url.pathname === '/api/v1/chat/sessions') {
    const body = await parseJsonBody(req)

    if (typeof body.machineId !== 'string' || body.machineId.length === 0) {
      sendJson(res, 400, { error: 'machineId is required.' })
      return
    }

    // A QR code may carry the serial number, which the README allows. The
    // AI service knows machines by machineId alone, so forward the resolved
    // identity rather than whatever the code happened to encode.
    body.machineId = (await authorizeMachineAccess(req, body.machineId, DOMAIN.COMMON)).machineId
    const idempotencyKey = readIdempotencyKey(req, body)
    req.idempotencyKey = idempotencyKey
    exposeIdempotencyKey(res, req)
    const session = await postAiJson(
      '/api/v1/chat/sessions',
      {
        machineId: body.machineId,
        ...(idempotencyKey ? { idempotencyKey } : {}),
      },
      aiServiceTimeoutMs,
      req,
      String(res.getHeader('X-Request-Id')),
    )
    exposeIdempotencyReplay(res, session)
    sendJson(res, 201, session)
    return
  }

  const sessionMatch = url.pathname.match(/^\/api\/v1\/chat\/sessions\/([^/]+)$/)
  if (req.method === 'GET' && sessionMatch) {
    const session = await getAiJson(
      `/api/v1/chat/sessions/${encodeURIComponent(sessionMatch[1])}`,
      req,
      String(res.getHeader('X-Request-Id')),
    )
    await authorizeMachineAccess(req, session.machineId, DOMAIN.COMMON)
    sendJson(res, 200, session)
    return
  }

  if (req.method === 'POST' && url.pathname === '/api/v1/chat/messages') {
    let body = await parseJsonBody(req, maxChatBodyBytes)
    const error = validateChatRequest(body)

    if (error) {
      sendJson(res, 400, { error })
      return
    }

    body = normalizeChatRequest(req, body)
    exposeIdempotencyKey(res, req)
    // A QR code may carry the serial number, which the README allows. The
    // AI service knows machines by machineId alone, so forward the resolved
    // identity rather than whatever the code happened to encode.
    body.machineId = (await authorizeMachineAccess(req, body.machineId, DOMAIN.COMMON)).machineId
    const chatResponse = await completeAndPersistChat(
      body,
      String(res.getHeader('X-Request-Id')),
      req,
    )
    exposeIdempotencyReplay(res, chatResponse)
    sendJson(res, 200, chatResponse)
    return
  }

  if (req.method === 'POST' && url.pathname === '/api/v1/escalations/draft') {
    let body = await parseJsonBody(req, maxChatBodyBytes)
    const error = validateChatRequest(body)

    if (error) {
      sendJson(res, 400, { error })
      return
    }

    body = normalizeChatRequest(req, body)
    exposeIdempotencyKey(res, req)
    // A QR code may carry the serial number, which the README allows. The
    // AI service knows machines by machineId alone, so forward the resolved
    // identity rather than whatever the code happened to encode.
    body.machineId = (await authorizeMachineAccess(req, body.machineId, DOMAIN.COMMON)).machineId
    const draft = await draftEscalation(body, req, String(res.getHeader('X-Request-Id')))
    exposeIdempotencyReplay(res, draft)
    if (!wasIdempotencyReplayed(draft)) {
      await recordEscalationAudit({
        request: body,
        draft,
        requestId: String(res.getHeader('X-Request-Id')),
        authContext: req.authContext,
      })
    }
    sendJson(res, 200, draft)
    return
  }

  if (req.method === 'GET' && url.pathname === '/api/v1/chat/stream') {
    res.setHeader('Allow', 'POST')
    sendJson(res, 405, { error: 'Use POST /api/v1/chat/stream.' })
    return
  }

  if (req.method === 'POST' && url.pathname === '/api/v1/chat/stream') {
    await streamChatRequest(await parseJsonBody(req, maxChatBodyBytes), req, res)
    return
  }

  if (req.method === 'GET' && url.pathname === '/api/v1/audit/events') {
    authorizeSupportAccess(req)
    sendJson(res, 200, {
      events: await listAuditEvents({
        limit: queryLimit(url),
        authContext: req.authContext,
      }),
    })
    return
  }

  if (req.method === 'GET' && url.pathname === '/api/v1/alerts/events') {
    authorizeSupportAccess(req)
    sendJson(res, 200, {
      events: await listAlertEvents({
        limit: queryLimit(url),
        authContext: req.authContext,
      }),
    })
    return
  }

  sendJson(res, 404, { error: 'Route not found.' })
}

const server = http.createServer((req, res) => {
  const startedAt = Date.now()
  const requestIdHeader = req.headers['x-request-id']
  const requestId = Array.isArray(requestIdHeader) ? requestIdHeader[0] : requestIdHeader || randomUUID()

  res.setHeader('X-Request-Id', requestId)
  res.on('finish', () => {
    const pathname = requestPath(req)
    recordHttpMetric({
      method: req.method,
      pathname,
      statusCode: res.statusCode,
      durationMs: Date.now() - startedAt,
    })
    console.log(
      JSON.stringify({
        requestId,
        method: req.method,
        path: pathname,
        statusCode: res.statusCode,
        durationMs: Date.now() - startedAt,
      }),
    )
  })

  route(req, res).catch((error) => {
    if (res.headersSent) {
      res.end()
      return
    }

    const statusCode = error.statusCode ?? 500
    const pathname = requestPath(req)
    if (statusCode >= 500 || statusCode === 429) {
      void recordAlert({
        type: statusCode === 429 ? 'gateway.rate-limit' : 'gateway.error',
        severity: statusCode === 429 ? 'medium' : 'high',
        requestId,
        machineId: undefined,
        message: statusCode === 429 ? 'Gateway rate limit exceeded.' : 'Gateway request failed.',
        metadata: {
          method: req.method,
          path: pathname,
          statusCode,
          detail: error.message,
        },
      }).catch((alertError) => {
        recordOperationsWriteFailure('alert')
        console.error(
          JSON.stringify({
            eventType: 'operations.write-failed',
            kind: 'alert',
            requestId,
            error: alertError instanceof Error ? alertError.name : 'Error',
          }),
        )
      })
    }

    // A coded refusal has already told the client everything it can act on, so
    // the prose is only a diagnosis - "JWT is not active yet", "Demo auth
    // cookie is required". That belongs in the log: the browser renders the
    // code, and an unauthenticated caller learning which check it tripped is
    // reconnaissance, not help.
    if (error.code) {
      console.warn(
        JSON.stringify({
          eventType: 'gateway.refused',
          requestId,
          statusCode,
          code: error.code,
          detail: error.message,
        }),
      )
    }

    sendJson(res, statusCode, {
      error: refusalText(statusCode, error),
      // A machine-readable code lets the UI tell "the source is down" apart
      // from "there is nothing here", which must never look the same.
      code: error.code,
      detail: statusCode === 500 && process.env.NODE_ENV !== 'production' ? error.message : undefined,
    })
  })
})

server.listen(port, () => {
  console.log(`Gateway service listening on http://localhost:${port}`)
})
