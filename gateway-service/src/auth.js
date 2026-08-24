import {
  createHash,
  createHmac,
  createPublicKey,
  generateKeyPairSync,
  randomBytes,
  sign,
  timingSafeEqual,
  verify,
} from 'node:crypto'
import {
  consumeOidcLoginState,
  consumeOidcLogoutState,
  createAuthSession,
  createOidcLoginState,
  createOidcLogoutState,
  deleteAuthSession,
  getAuthSession,
  getOidcLogoutState,
} from './operations.js'

const DEFAULT_JWKS_CACHE_MS = 300000
const DEFAULT_CLOCK_TOLERANCE_SECONDS = 60
const DEFAULT_MACHINE_IDS_CLAIM = 'machine_ids'
const DEFAULT_USER_ID_CLAIM = 'user_id'
const DEFAULT_COMPANY_ID_CLAIM = 'company_id'
const DEFAULT_VISIBILITY_CLAIM = 'visibility'
const DEFAULT_ROLES_CLAIM = 'roles'
const DEFAULT_ADMIN_ROLES = ['arol-admin', 'arol-support']
const DEFAULT_ALLOWED_ALGORITHMS = ['RS256']
const DEFAULT_AUTH_COOKIE_NAME = '__Host-arol_q2_access'
const DEFAULT_DEMO_ISSUER = 'https://arol-q2.local/demo-idp'
const DEFAULT_DEMO_AUDIENCE = 'arol-q2-gateway'
const DEFAULT_DEMO_SUBJECT = 'demo-operator'
// The identity a demo sign-in uses when none is chosen. A real dataset user
// rather than a synthetic subject, so it carries a company and a visibility
// and is governed by the same access model as any other sign-in.
const DEFAULT_DEMO_USER_ID = 'USR-011'
const DEFAULT_DEMO_ROLES = ['operator']
const DEFAULT_DEMO_MACHINE_IDS = [
  // The supplied fleet. Machine scoping is a placeholder for the dataset's
  // real access model, where a user reaches their own company's machines.
  'MCH-0001', 'MCH-0002', 'MCH-0003', 'MCH-0004',
  'MCH-0005', 'MCH-0006', 'MCH-0007', 'MCH-0008',
]
const DEFAULT_DEMO_TOKEN_TTL_SECONDS = 28800
const DEFAULT_CSRF_HEADER_NAME = 'x-csrf-token'
const DEFAULT_OIDC_SCOPES = ['openid', 'profile', 'email']
const DEFAULT_OIDC_LOGIN_STATE_TTL_SECONDS = 600
const DEFAULT_AUTH_SESSION_TTL_SECONDS = 28800
const DEFAULT_AUTH_SESSION_REFRESH_WINDOW_SECONDS = 120
const DEFAULT_OIDC_REQUEST_TIMEOUT_MS = 5000
const DEMO_KEY_ID = 'arol-q2-demo-key'

const jwksCache = new Map()
const discoveryCache = new Map()
const refreshFlights = new Map()
const demoKeyPair = generateKeyPairSync('rsa', { modulusLength: 2048 })
const demoPublicJwk = {
  ...demoKeyPair.publicKey.export({ format: 'jwk' }),
  kid: DEMO_KEY_ID,
  alg: 'RS256',
  use: 'sig',
}

export function buildAuthConfig(env = process.env) {
  // Authenticated by default. `off` disables the access model entirely, so it
  // has to be asked for explicitly rather than being what you get by omission.
  const mode = env.GATEWAY_AUTH_MODE ?? 'demo'

  if (mode === 'off') {
    console.warn(
      JSON.stringify({
        eventType: 'auth.disabled',
        message:
          'GATEWAY_AUTH_MODE=off: authentication and the company/visibility access '
          + 'model are disabled. Local development only.',
      }),
    )
  }

  return {
    mode,
    issuer: trimTrailingSlash(
      mode === 'demo' ? env.OIDC_ISSUER || DEFAULT_DEMO_ISSUER : env.OIDC_ISSUER ?? '',
    ),
    audience: mode === 'demo' ? env.OIDC_AUDIENCE || DEFAULT_DEMO_AUDIENCE : env.OIDC_AUDIENCE ?? '',
    jwksUri: env.OIDC_JWKS_URI ?? '',
    clientId: env.OIDC_CLIENT_ID ?? '',
    clientSecret: env.OIDC_CLIENT_SECRET ?? '',
    redirectUri: env.OIDC_REDIRECT_URI ?? '',
    postLogoutRedirectUri: env.OIDC_POST_LOGOUT_REDIRECT_URI ?? '',
    scopes: parseCsv(env.OIDC_SCOPES, DEFAULT_OIDC_SCOPES),
    allowedAlgorithms: parseCsv(env.OIDC_ALLOWED_ALGORITHMS, DEFAULT_ALLOWED_ALGORITHMS),
    machineIdsClaim: env.OIDC_MACHINE_IDS_CLAIM || DEFAULT_MACHINE_IDS_CLAIM,
    userIdClaim: env.OIDC_USER_ID_CLAIM || DEFAULT_USER_ID_CLAIM,
    companyIdClaim: env.OIDC_COMPANY_ID_CLAIM || DEFAULT_COMPANY_ID_CLAIM,
    visibilityClaim: env.OIDC_VISIBILITY_CLAIM || DEFAULT_VISIBILITY_CLAIM,
    rolesClaim: env.OIDC_ROLES_CLAIM || DEFAULT_ROLES_CLAIM,
    adminRoles: parseCsv(env.OIDC_ADMIN_ROLES, DEFAULT_ADMIN_ROLES),
    deploymentMachineIds: new Set(parseCsv(env.GATEWAY_MACHINE_IDS)),
    authCookieName: env.GATEWAY_AUTH_COOKIE_NAME || DEFAULT_AUTH_COOKIE_NAME,
    authCookieSecure: truthy(env.GATEWAY_AUTH_COOKIE_SECURE, (env.GATEWAY_AUTH_COOKIE_NAME || DEFAULT_AUTH_COOKIE_NAME).startsWith('__Host-')),
    jwksCacheMs: Number(env.OIDC_JWKS_CACHE_MS ?? DEFAULT_JWKS_CACHE_MS),
    clockToleranceSeconds: Number(env.OIDC_CLOCK_TOLERANCE_SECONDS ?? DEFAULT_CLOCK_TOLERANCE_SECONDS),
    demoSubject: env.GATEWAY_AUTH_DEMO_SUBJECT || DEFAULT_DEMO_SUBJECT,
    demoUserId: env.GATEWAY_AUTH_DEMO_USER_ID || DEFAULT_DEMO_USER_ID,
    demoRoles: parseCsv(env.GATEWAY_AUTH_DEMO_ROLES, DEFAULT_DEMO_ROLES),
    demoMachineIds: parseCsv(env.GATEWAY_AUTH_DEMO_MACHINE_IDS, DEFAULT_DEMO_MACHINE_IDS),
    demoTokenTtlSeconds: Number(env.GATEWAY_AUTH_DEMO_TOKEN_TTL_SECONDS ?? DEFAULT_DEMO_TOKEN_TTL_SECONDS),
    oidcStateCookieName: env.OIDC_STATE_COOKIE_NAME || '__Host-arol_q2_oidc_state',
    oidcLogoutStateCookieName: env.OIDC_LOGOUT_STATE_COOKIE_NAME || '__Host-arol_q2_oidc_logout',
    oidcLoginStateTtlSeconds: boundedInteger(
      env.OIDC_LOGIN_STATE_TTL_SECONDS,
      DEFAULT_OIDC_LOGIN_STATE_TTL_SECONDS,
      60,
      900,
    ),
    authSessionTtlSeconds: boundedInteger(
      env.GATEWAY_AUTH_SESSION_TTL_SECONDS,
      DEFAULT_AUTH_SESSION_TTL_SECONDS,
      300,
      7 * 24 * 60 * 60,
    ),
    authSessionRefreshWindowSeconds: boundedInteger(
      env.GATEWAY_AUTH_SESSION_REFRESH_WINDOW_SECONDS,
      DEFAULT_AUTH_SESSION_REFRESH_WINDOW_SECONDS,
      30,
      900,
    ),
    oidcRequestTimeoutMs: boundedInteger(
      env.OIDC_REQUEST_TIMEOUT_MS,
      DEFAULT_OIDC_REQUEST_TIMEOUT_MS,
      100,
      30000,
    ),
    csrfSecret:
      env.GATEWAY_CSRF_SECRET
      || env.AI_SERVICE_SHARED_SECRET
      || (mode === 'demo' ? `${DEFAULT_DEMO_ISSUER}:csrf` : ''),
    csrfHeaderName: (env.GATEWAY_CSRF_HEADER_NAME || DEFAULT_CSRF_HEADER_NAME).toLowerCase(),
  }
}

export function authConfigurationReadiness(config) {
  const supportedModes = new Set(['off', 'demo', 'oidc'])
  if (!supportedModes.has(config.mode)) {
    return {
      status: 'unavailable',
      required: true,
      mode: config.mode,
      missing: ['supported GATEWAY_AUTH_MODE'],
    }
  }

  if (config.mode === 'off') {
    return {
      status: 'ready',
      required: true,
      mode: 'off',
      warning: 'authentication is explicitly disabled',
    }
  }

  const missing = []
  if (!config.csrfSecret) {
    missing.push('GATEWAY_CSRF_SECRET')
  }

  if (config.mode === 'oidc') {
    if (!config.issuer) {
      missing.push('OIDC_ISSUER')
    }
    if (!config.audience) {
      missing.push('OIDC_AUDIENCE')
    }
    if (!config.clientId) {
      missing.push('OIDC_CLIENT_ID')
    }
    if (!config.redirectUri) {
      missing.push('OIDC_REDIRECT_URI')
    }
  }

  return {
    status: missing.length === 0 ? 'ready' : 'unavailable',
    required: true,
    mode: config.mode,
    ...(missing.length > 0 ? { missing } : {}),
  }
}

export async function authenticateRequest(req, config) {
  if (config.mode === 'off') {
    return {
      mode: 'off',
      subject: 'anonymous',
      machineIds: config.deploymentMachineIds,
      hasMachineRestriction: config.deploymentMachineIds.size > 0,
    }
  }

  if (config.mode === 'demo') {
    const token = getPresentedToken(req, config)

    if (!token) {
      throw httpError(401, 'Demo auth cookie is required.', 'session_expired')
    }

    return authContextFromClaims(await verifyJwt(token, config), config, 'demo')
  }

  if (config.mode !== 'oidc') {
    throw httpError(500, `Unsupported gateway auth mode: ${config.mode}`)
  }

  assertOidcConfigured(config)

  const bearerToken = getBearerToken(req)
  if (bearerToken) {
    return authContextFromClaims(await verifyJwt(bearerToken, config), config, 'oidc')
  }

  const sessionId = getCookieValue(req, config.authCookieName)

  if (!sessionId) {
    throw httpError(401, 'A valid OIDC JWT is required.', 'session_expired')
  }

  const session = await getAuthSession(sessionId)
  if (!session) {
    // Keep accepting pre-BFF JWT cookies during a rolling deployment. New login
    // responses always set an opaque session identifier instead.
    if (sessionId.split('.').length === 3) {
      return authContextFromClaims(await verifyJwt(sessionId, config), config, 'oidc')
    }

    throw httpError(401, 'OIDC session is missing or expired.', 'session_expired')
  }

  const activeSession = await ensureActiveAuthSession(session, config)
  return {
    ...authContextFromClaims(activeSession.claims, config, 'oidc'),
    sessionId: activeSession.sessionId,
  }
}

export async function beginOidcLogin(config, returnTo) {
  assertOidcConfigured(config)
  assertOidcClientConfigured(config)

  const configuration = await getOidcConfiguration(config)
  if (typeof configuration.authorization_endpoint !== 'string') {
    throw httpError(503, 'OIDC discovery did not include authorization_endpoint.', 'sign_in_unavailable')
  }

  const state = randomBytes(32).toString('base64url')
  const nonce = randomBytes(32).toString('base64url')
  const codeVerifier = randomBytes(64).toString('base64url')
  const codeChallenge = createHash('sha256').update(codeVerifier).digest('base64url')
  const expiresAt = new Date(Date.now() + config.oidcLoginStateTtlSeconds * 1000).toISOString()

  await createOidcLoginState({ state, nonce, codeVerifier, returnTo, expiresAt })

  const authorizationUrl = new URL(configuration.authorization_endpoint)
  authorizationUrl.search = new URLSearchParams({
    client_id: config.clientId,
    redirect_uri: config.redirectUri,
    response_type: 'code',
    scope: config.scopes.join(' '),
    state,
    nonce,
    code_challenge: codeChallenge,
    code_challenge_method: 'S256',
  }).toString()

  return {
    state,
    authorizationUrl: authorizationUrl.toString(),
    maxAge: config.oidcLoginStateTtlSeconds,
  }
}

export async function completeOidcLogin({ config, state, code }) {
  assertOidcConfigured(config)
  assertOidcClientConfigured(config)

  const loginState = await consumeOidcLoginState(state)
  if (!loginState) {
    throw httpError(400, 'OIDC login state is missing or expired.', 'sign_in_failed')
  }

  const tokenResponse = await exchangeAuthorizationCode({ config, code, codeVerifier: loginState.codeVerifier })
  const claims = await verifyOidcIdToken(tokenResponse.id_token, config, loginState.nonce)
  const now = new Date().toISOString()
  const session = {
    sessionId: randomBytes(32).toString('base64url'),
    subject: String(claims.sub),
    accessToken: tokenResponse.access_token,
    refreshToken: tokenResponse.refresh_token ?? null,
    idToken: tokenResponse.id_token,
    accessExpiresAt: tokenExpiry(tokenResponse, claims),
    expiresAt: new Date(Date.now() + config.authSessionTtlSeconds * 1000).toISOString(),
    createdAt: now,
    lastUsedAt: now,
    claims,
  }

  await createAuthSession(session)
  return { session, returnTo: loginState.returnTo }
}

export async function beginOidcLogout({ config, sessionId, returnTo }) {
  if (config.mode !== 'oidc' || !sessionId || !config.postLogoutRedirectUri) {
    return null
  }

  const session = await getAuthSession(sessionId)
  if (!session?.idToken) {
    return null
  }

  let configuration
  try {
    configuration = await getOidcConfiguration(config)
  } catch {
    return null
  }

  if (typeof configuration.end_session_endpoint !== 'string' || !configuration.end_session_endpoint) {
    return null
  }

  const state = randomBytes(32).toString('base64url')
  const expiresAt = new Date(Date.now() + config.oidcLoginStateTtlSeconds * 1000).toISOString()
  await createOidcLogoutState({
    state,
    idToken: session.idToken,
    returnTo: returnTo || '/',
    expiresAt,
  })

  return { state, maxAge: config.oidcLoginStateTtlSeconds }
}

export async function oidcLogoutRedirect({ config, state }) {
  if (!state || !config.postLogoutRedirectUri) {
    return null
  }

  const logoutState = await getOidcLogoutState(state)
  if (!logoutState) {
    return null
  }

  let configuration
  try {
    configuration = await getOidcConfiguration(config)
  } catch {
    return null
  }

  if (typeof configuration.end_session_endpoint !== 'string' || !configuration.end_session_endpoint) {
    return null
  }

  const logoutUrl = new URL(configuration.end_session_endpoint)
  logoutUrl.search = new URLSearchParams({
    id_token_hint: logoutState.idToken,
    client_id: config.clientId,
    post_logout_redirect_uri: config.postLogoutRedirectUri,
    state,
  }).toString()
  return logoutUrl.toString()
}

export async function completeOidcLogout(state) {
  const logoutState = await consumeOidcLogoutState(state)
  return logoutState?.returnTo ?? '/'
}

export async function logoutAuthSession(sessionId) {
  if (sessionId) {
    await deleteAuthSession(sessionId)
  }
}

async function ensureActiveAuthSession(session, config) {
  const now = Date.now()
  const sessionExpiresAt = Date.parse(session.expiresAt)
  const accessExpiresAt = Date.parse(session.accessExpiresAt)
  const refreshExpiresAt = session.refreshExpiresAt ? Date.parse(session.refreshExpiresAt) : Number.POSITIVE_INFINITY

  if (!Number.isFinite(sessionExpiresAt) || sessionExpiresAt <= now) {
    await logoutAuthSession(session.sessionId)
    throw httpError(401, 'OIDC session is expired.', 'session_expired')
  }

  if (Number.isFinite(accessExpiresAt) && accessExpiresAt - now <= config.authSessionRefreshWindowSeconds * 1000) {
    if (!session.refreshToken || refreshExpiresAt <= now) {
      if (accessExpiresAt <= now) {
        await logoutAuthSession(session.sessionId)
        throw httpError(401, 'OIDC access token is expired.', 'session_expired')
      }

      return session
    }

    try {
      return await refreshAuthSessionSingleFlight(session, config)
    } catch (error) {
      if (accessExpiresAt > now) {
        return session
      }

      await logoutAuthSession(session.sessionId)
      throw error
    }
  }

  return session
}

async function refreshAuthSessionSingleFlight(session, config) {
  const existing = refreshFlights.get(session.sessionId)
  if (existing) {
    return existing
  }

  const refresh = refreshAuthSession(session, config).finally(() => {
    if (refreshFlights.get(session.sessionId) === refresh) {
      refreshFlights.delete(session.sessionId)
    }
  })
  refreshFlights.set(session.sessionId, refresh)
  return refresh
}

async function refreshAuthSession(session, config) {
  const tokenResponse = await exchangeRefreshToken({
    config,
    refreshToken: session.refreshToken,
  })
  let claims = session.claims

  if (tokenResponse.id_token) {
    const refreshedClaims = await verifyOidcIdToken(tokenResponse.id_token, config)
    if (String(refreshedClaims.sub) !== String(session.claims.sub)) {
      throw httpError(401, 'OIDC refresh returned a different subject.', 'session_expired')
    }
    claims = refreshedClaims
  }

  const refreshedSession = {
    ...session,
    accessToken: tokenResponse.access_token,
    refreshToken: tokenResponse.refresh_token ?? session.refreshToken,
    idToken: tokenResponse.id_token ?? session.idToken,
    accessExpiresAt: tokenExpiry(tokenResponse, claims),
    expiresAt: new Date(Date.now() + config.authSessionTtlSeconds * 1000).toISOString(),
    lastUsedAt: new Date().toISOString(),
    claims,
  }
  await createAuthSession(refreshedSession)
  return refreshedSession
}

async function exchangeAuthorizationCode({ config, code, codeVerifier }) {
  const configuration = await getOidcConfiguration(config)
  if (typeof configuration.token_endpoint !== 'string') {
    throw httpError(503, 'OIDC discovery did not include token_endpoint.', 'sign_in_unavailable')
  }

  const form = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    redirect_uri: config.redirectUri,
    client_id: config.clientId,
    code_verifier: codeVerifier,
  })
  if (config.clientSecret) {
    form.set('client_secret', config.clientSecret)
  }

  return requestOidcToken(configuration.token_endpoint, form, config.oidcRequestTimeoutMs)
}

async function exchangeRefreshToken({ config, refreshToken }) {
  const configuration = await getOidcConfiguration(config)
  if (typeof configuration.token_endpoint !== 'string') {
    throw httpError(503, 'OIDC discovery did not include token_endpoint.', 'sign_in_unavailable')
  }

  const form = new URLSearchParams({
    grant_type: 'refresh_token',
    refresh_token: refreshToken,
    client_id: config.clientId,
  })
  if (config.clientSecret) {
    form.set('client_secret', config.clientSecret)
  }

  return requestOidcToken(configuration.token_endpoint, form, config.oidcRequestTimeoutMs)
}

async function requestOidcToken(tokenEndpoint, form, timeoutMs) {
  let response
  try {
    response = await fetch(tokenEndpoint, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: form.toString(),
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch (error) {
    throw httpError(503, `OIDC token endpoint is unavailable: ${errorMessage(error)}`)
  }

  let payload
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    throw httpError(401, 'OIDC token exchange was rejected.', 'session_expired')
  }

  if (!payload || typeof payload.access_token !== 'string' || !payload.access_token) {
    throw httpError(503, 'OIDC token response did not include access_token.', 'sign_in_unavailable')
  }

  return payload
}

async function verifyOidcIdToken(idToken, config, expectedNonce) {
  if (typeof idToken !== 'string' || !idToken) {
    throw httpError(401, 'OIDC token response did not include id_token.', 'session_expired')
  }

  const claims = await verifyJwt(idToken, { ...config, audience: config.clientId })
  if (expectedNonce !== undefined && String(claims.nonce ?? '') !== expectedNonce) {
    throw httpError(401, 'OIDC ID token nonce is invalid.', 'session_expired')
  }

  if (Array.isArray(claims.aud) && claims.aud.length > 1 && claims.azp !== config.clientId) {
    throw httpError(401, 'OIDC ID token authorized party is invalid.', 'session_expired')
  }

  return claims
}

function tokenExpiry(tokenResponse, claims) {
  const expiresIn = Number(tokenResponse.expires_in)
  if (Number.isFinite(expiresIn) && expiresIn > 0) {
    return new Date(Date.now() + expiresIn * 1000).toISOString()
  }

  if (typeof claims.exp === 'number') {
    return new Date(claims.exp * 1000).toISOString()
  }

  return new Date(Date.now() + DEFAULT_AUTH_SESSION_REFRESH_WINDOW_SECONDS * 1000).toISOString()
}

/**
 * Mint a demo token.
 *
 * `identity` carries the signed-in dataset user. The demo has no passwords by
 * design: authentication is not what this project is assessed on, and the
 * dataset states every account is active. Authorization is what matters, so the
 * user's companyId and visibility are put in the token and enforced from there.
 */
export function createDemoJwt(config, identity = null) {
  if (config.mode !== 'demo') {
    throw httpError(404, 'Demo auth is not enabled.', 'demo_disabled')
  }

  const now = Math.floor(Date.now() / 1000)
  const claims = {
    iss: config.issuer,
    aud: config.audience,
    sub: identity?.subject ?? config.demoSubject,
    [config.rolesClaim]: identity?.roles ?? config.demoRoles,
    [config.machineIdsClaim]: identity?.machineIds ?? config.demoMachineIds,
    ...(identity?.userId ? { [config.userIdClaim]: identity.userId } : {}),
    ...(identity?.companyId ? { [config.companyIdClaim]: identity.companyId } : {}),
    ...(identity?.visibility ? { [config.visibilityClaim]: identity.visibility } : {}),
    iat: now,
    nbf: now,
    exp: now + config.demoTokenTtlSeconds,
  }

  const encodedHeader = encodeJwtPart({
    alg: 'RS256',
    typ: 'JWT',
    kid: DEMO_KEY_ID,
  })
  const encodedPayload = encodeJwtPart(claims)
  const signingInput = `${encodedHeader}.${encodedPayload}`
  const signature = sign('RSA-SHA256', Buffer.from(signingInput), demoKeyPair.privateKey)

  return `${signingInput}.${signature.toString('base64url')}`
}

function authContextFromClaims(claims, config, mode) {
  const roles = claimValues(claims[config.rolesClaim])
  const isAdmin = roles.some((role) => config.adminRoles.includes(role))
  const tokenMachineIds = new Set(claimValues(claims[config.machineIdsClaim]))
  const machineIds = allowedMachineIds(tokenMachineIds, config.deploymentMachineIds, isAdmin)

  return {
    mode,
    subject: String(claims.sub ?? ''),
    issuer: claims.iss,
    audience: claims.aud,
    roles,
    isAdmin,
    machineIds,
    hasMachineRestriction: !isAdmin || config.deploymentMachineIds.size > 0,
    // The two checks of the access model. Both come from the token so that no
    // request can widen its own scope.
    userId: singleClaim(claims[config.userIdClaim]),
    companyId: singleClaim(claims[config.companyIdClaim]),
    visibility: singleClaim(claims[config.visibilityClaim]),
    claims,
  }
}

function singleClaim(value) {
  if (typeof value === 'string' && value.trim()) {
    return value.trim()
  }
  return null
}

export function authorizeMachine(authContext, machineId) {
  if (!authContext.hasMachineRestriction || authContext.machineIds.has(machineId)) {
    return
  }

  throw httpError(403, 'Machine access is not allowed for this identity.', 'machine_not_allowed')
}

export function authRateLimitKey(req) {
  const subject = req.authContext?.subject

  if (subject) {
    return `sub:${subject}`
  }

  const configuredHops = Number(process.env.GATEWAY_TRUST_PROXY_HOPS ?? 0)
  const trustedProxyHops =
    Number.isInteger(configuredHops) && configuredHops > 0 ? configuredHops : 0
  const forwardedHeader = req.headers['x-forwarded-for']
  const forwardedFor = (Array.isArray(forwardedHeader) ? forwardedHeader.join(',') : forwardedHeader ?? '')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean)
  const forwardedIndex = forwardedFor.length - trustedProxyHops
  const trustedAddress =
    trustedProxyHops > 0 && forwardedIndex >= 0 ? forwardedFor[forwardedIndex] : undefined

  return `ip:${trustedAddress || req.socket.remoteAddress || 'unknown'}`
}

export function getPresentedToken(req, config) {
  return getBearerToken(req) || getCookieValue(req, config.authCookieName)
}

export function getCookieAuthToken(req, config) {
  return getBearerToken(req) ? undefined : getCookieValue(req, config.authCookieName)
}

export function csrfTokenForAuthToken(authToken, config) {
  assertCsrfConfigured(config)
  return `csrf-v1.${createHmac('sha256', config.csrfSecret).update(authToken).digest('base64url')}`
}

export function requireCsrfForCookieAuth(req, config) {
  if (config.mode === 'off' || isSafeMethod(req.method)) {
    return
  }

  const authToken = getCookieAuthToken(req, config)
  if (!authToken) {
    return
  }

  const expected = csrfTokenForAuthToken(authToken, config)
  const providedHeader = req.headers[config.csrfHeaderName]
  const provided = Array.isArray(providedHeader) ? providedHeader[0] : providedHeader

  if (!provided || !safeEqual(provided, expected)) {
    throw httpError(403, 'A valid CSRF token is required for cookie-authenticated requests.', 'csrf_required')
  }
}

export function getBearerToken(req) {
  const authorization = req.headers.authorization
  const value = Array.isArray(authorization) ? authorization[0] : authorization
  const match = value?.match(/^Bearer\s+(.+)$/i)

  return match?.[1]
}

export function getCookieValue(req, name) {
  const header = req.headers.cookie
  const value = Array.isArray(header) ? header.join('; ') : header

  if (!value) {
    return undefined
  }

  for (const pair of value.split(';')) {
    const separatorIndex = pair.indexOf('=')

    if (separatorIndex === -1) {
      continue
    }

    const key = pair.slice(0, separatorIndex).trim()
    const rawCookieValue = pair.slice(separatorIndex + 1).trim()

    if (key === name) {
      try {
        return decodeURIComponent(rawCookieValue)
      } catch {
        return undefined
      }
    }
  }

  return undefined
}

export async function verifyJwt(token, config) {
  const parts = token.split('.')

  if (parts.length !== 3) {
    throw httpError(401, 'Bearer token must be a compact JWT.', 'session_expired')
  }

  const [encodedHeader, encodedPayload, encodedSignature] = parts
  const header = parseJsonPart(encodedHeader, 'JWT header')
  const claims = parseJsonPart(encodedPayload, 'JWT payload')

  if (!config.allowedAlgorithms.includes(header.alg)) {
    throw httpError(401, `JWT algorithm ${header.alg ?? 'unknown'} is not allowed.`)
  }

  if (!header.kid) {
    throw httpError(401, 'JWT header must include kid.', 'session_expired')
  }

  validateClaims(claims, config)

  const jwk = await findSigningKey(header.kid, config)
  const publicKey = createPublicKey({ key: jwk, format: 'jwk' })
  const signingInput = Buffer.from(`${encodedHeader}.${encodedPayload}`)
  const signature = Buffer.from(encodedSignature, 'base64url')

  if (!verifySignature(header.alg, signingInput, publicKey, signature)) {
    throw httpError(401, 'JWT signature is invalid.', 'session_expired')
  }

  return claims
}

async function findSigningKey(kid, config) {
  if (config.mode === 'demo') {
    if (kid === DEMO_KEY_ID) {
      return demoPublicJwk
    }

    throw httpError(401, 'Demo JWT signing key was not found.', 'session_expired')
  }

  const jwks = await getJwks(config)
  const key = jwks.keys?.find((item) => item.kid === kid)

  if (!key) {
    jwksCache.clear()
    const refreshedJwks = await getJwks(config)
    const refreshedKey = refreshedJwks.keys?.find((item) => item.kid === kid)

    if (refreshedKey) {
      return refreshedKey
    }

    throw httpError(401, 'JWT signing key was not found in JWKS.', 'session_expired')
  }

  return key
}

async function getJwks(config) {
  const jwksUri = config.jwksUri || (await discoverJwksUri(config))
  const cacheKey = jwksUri
  const now = Date.now()
  const cached = jwksCache.get(cacheKey)

  if (cached && cached.expiresAt > now) {
    return cached.jwks
  }

  const response = await fetch(jwksUri, {
    headers: {
      Accept: 'application/json',
    },
    signal: AbortSignal.timeout(config.oidcRequestTimeoutMs),
  }).catch((error) => {
    throw httpError(503, `OIDC JWKS is unavailable: ${errorMessage(error)}`)
  })

  if (!response.ok) {
    throw httpError(503, `OIDC JWKS returned ${response.status}.`)
  }

  const jwks = await response.json()
  jwksCache.set(cacheKey, {
    jwks,
    expiresAt: now + config.jwksCacheMs,
  })

  return jwks
}

export async function getOidcConfiguration(config) {
  assertOidcConfigured(config)

  const now = Date.now()
  const cached = discoveryCache.get(config.issuer)
  if (cached && cached.expiresAt > now) {
    return cached.configuration
  }

  const discoveryUrl = `${config.issuer}/.well-known/openid-configuration`
  const response = await fetch(discoveryUrl, {
    headers: {
      Accept: 'application/json',
    },
    signal: AbortSignal.timeout(config.oidcRequestTimeoutMs),
  }).catch((error) => {
    throw httpError(503, `OIDC discovery is unavailable: ${errorMessage(error)}`)
  })

  if (!response.ok) {
    throw httpError(503, `OIDC discovery returned ${response.status}.`)
  }

  const configuration = await response.json()

  if (trimTrailingSlash(configuration.issuer ?? '') !== config.issuer) {
    throw httpError(503, 'OIDC discovery issuer does not match OIDC_ISSUER.', 'sign_in_unavailable')
  }

  if (typeof configuration.jwks_uri !== 'string' || !configuration.jwks_uri) {
    throw httpError(503, 'OIDC discovery did not include jwks_uri.', 'sign_in_unavailable')
  }

  discoveryCache.set(config.issuer, {
    configuration,
    expiresAt: now + config.jwksCacheMs,
  })

  return configuration
}

async function discoverJwksUri(config) {
  return (await getOidcConfiguration(config)).jwks_uri
}

function validateClaims(claims, config) {
  const now = Math.floor(Date.now() / 1000)
  const tolerance = config.clockToleranceSeconds

  if (trimTrailingSlash(claims.iss ?? '') !== config.issuer) {
    throw httpError(401, 'JWT issuer is invalid.', 'session_expired')
  }

  if (!audienceMatches(claims.aud, config.audience)) {
    throw httpError(401, 'JWT audience is invalid.', 'session_expired')
  }

  if (!claims.sub) {
    throw httpError(401, 'JWT subject is required.', 'session_expired')
  }

  if (typeof claims.exp !== 'number' || claims.exp + tolerance < now) {
    throw httpError(401, 'JWT is expired.', 'session_expired')
  }

  if (typeof claims.nbf === 'number' && claims.nbf - tolerance > now) {
    throw httpError(401, 'JWT is not active yet.', 'session_expired')
  }
}

function verifySignature(alg, signingInput, publicKey, signature) {
  if (alg.startsWith('ES')) {
    return verify(jwtHashAlgorithm(alg), signingInput, { key: publicKey, dsaEncoding: 'ieee-p1363' }, signature)
  }

  return verify(jwtHashAlgorithm(alg), signingInput, publicKey, signature)
}

function jwtHashAlgorithm(alg) {
  switch (alg) {
    case 'RS256':
      return 'RSA-SHA256'
    case 'RS384':
      return 'RSA-SHA384'
    case 'RS512':
      return 'RSA-SHA512'
    case 'ES256':
      return 'SHA256'
    case 'ES384':
      return 'SHA384'
    case 'ES512':
      return 'SHA512'
    default:
      throw httpError(401, `JWT algorithm ${alg} is not supported.`)
  }
}

function allowedMachineIds(tokenMachineIds, deploymentMachineIds, isAdmin) {
  if (isAdmin) {
    return deploymentMachineIds.size > 0 ? deploymentMachineIds : new Set()
  }

  if (deploymentMachineIds.size === 0) {
    return tokenMachineIds
  }

  return new Set([...tokenMachineIds].filter((machineId) => deploymentMachineIds.has(machineId)))
}

function audienceMatches(audienceClaim, expectedAudience) {
  if (Array.isArray(audienceClaim)) {
    return audienceClaim.some((value) => safeEqual(String(value), expectedAudience))
  }

  return safeEqual(String(audienceClaim ?? ''), expectedAudience)
}

function claimValues(value) {
  if (Array.isArray(value)) {
    return value.map(String).filter(Boolean)
  }

  if (typeof value === 'string') {
    return parseCsv(value)
  }

  return []
}

function parseJsonPart(value, label) {
  try {
    return JSON.parse(Buffer.from(value, 'base64url').toString('utf8'))
  } catch {
    throw httpError(401, `${label} must be valid base64url JSON.`)
  }
}

function assertOidcConfigured(config) {
  if (!config.issuer) {
    throw httpError(500, 'OIDC auth is enabled but OIDC_ISSUER is not set.', 'server_misconfigured')
  }

  if (!config.audience) {
    throw httpError(500, 'OIDC auth is enabled but OIDC_AUDIENCE is not set.', 'server_misconfigured')
  }
}

function assertOidcClientConfigured(config) {
  if (!config.clientId) {
    throw httpError(500, 'OIDC login is enabled but OIDC_CLIENT_ID is not set.', 'server_misconfigured')
  }

  if (!config.redirectUri) {
    throw httpError(500, 'OIDC login is enabled but OIDC_REDIRECT_URI is not set.', 'server_misconfigured')
  }
}

function assertCsrfConfigured(config) {
  if (!config.csrfSecret) {
    throw httpError(500, 'Cookie auth is enabled but GATEWAY_CSRF_SECRET is not set.', 'server_misconfigured')
  }
}

function isSafeMethod(method) {
  return ['GET', 'HEAD', 'OPTIONS'].includes(String(method ?? 'GET').toUpperCase())
}

function parseCsv(value, fallback = []) {
  const source = value ?? fallback.join(',')

  return source
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function boundedInteger(value, fallback, minimum, maximum) {
  const parsed = Number(value ?? fallback)
  if (!Number.isInteger(parsed)) {
    return fallback
  }

  return Math.min(Math.max(parsed, minimum), maximum)
}

function truthy(value, fallback = false) {
  if (value === undefined || value === null || value === '') {
    return fallback
  }

  return ['1', 'true', 'yes', 'on'].includes(String(value).toLowerCase())
}

function encodeJwtPart(value) {
  return Buffer.from(JSON.stringify(value)).toString('base64url')
}

function safeEqual(left, right) {
  const leftBuffer = Buffer.from(left)
  const rightBuffer = Buffer.from(right)

  if (leftBuffer.length !== rightBuffer.length) {
    return false
  }

  return timingSafeEqual(leftBuffer, rightBuffer)
}

function trimTrailingSlash(value) {
  return value.replace(/\/+$/, '')
}

/**
 * An auth failure, with the reason split from the wording.
 *
 * `message` says what went wrong in the terms the log needs - which claim, which
 * key, which endpoint. The browser must never print it: "JWT is not active yet"
 * tells a line operator nothing they can act on, and naming the failing token
 * field is free reconnaissance. `code` is the part the client branches on, and
 * the wording it shows is written for the person holding the phone.
 */
function httpError(statusCode, message, code) {
  const error = new Error(message)
  error.statusCode = statusCode
  if (code) {
    error.code = code
  }
  return error
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error)
}
