import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash, generateKeyPairSync, sign } from 'node:crypto'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import http from 'node:http'
import net from 'node:net'
import { once } from 'node:events'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { DatabaseSync } from 'node:sqlite'
import test from 'node:test'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const gatewayRoot = path.resolve(__dirname, '..')
const repoRoot = path.resolve(gatewayRoot, '..')
const authCookieName = '__Host-arol_q2_access'

test('gateway exposes telemetry, business, authz, and AI forwarding behavior', async (t) => {
  const observedAiRequests = []
  const observedTelemetryRequests = []
  const seenAiIdempotencyKeys = new Set()
  const operationsDir = await mkdtemp(path.join(os.tmpdir(), 'arol-q2-ops-'))
  const issuer = 'https://identity.example.test'
  const audience = 'arol-q2-gateway'
  const machineId = 'MCH-0004'
  const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 })
  const publicJwk = {
    ...publicKey.export({ format: 'jwk' }),
    kid: 'gateway-test-key',
    alg: 'RS256',
    use: 'sig',
  }
  const aiServer = http.createServer(async (req, res) => {
    const chunks = []
    for await (const chunk of req) {
      chunks.push(chunk)
    }
    const bodyText = Buffer.concat(chunks).toString('utf8')
    const body = parseJson(bodyText)
    const idempotencyKey = req.headers['idempotency-key']
    const scopedIdempotencyKey = idempotencyKey ? `${req.url}:${idempotencyKey}` : null
    const idempotencyWasReplayed = Boolean(
      scopedIdempotencyKey && seenAiIdempotencyKeys.has(scopedIdempotencyKey),
    )
    if (scopedIdempotencyKey) {
      seenAiIdempotencyKeys.add(scopedIdempotencyKey)
    }

    observedAiRequests.push({
      path: req.url,
      requestId: req.headers['x-request-id'],
      headers: req.headers,
      body: bodyText,
    })

    if (req.url === '/.well-known/openid-configuration') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(
        JSON.stringify({
          issuer,
          jwks_uri: `http://127.0.0.1:${aiServer.address().port}/oauth2/jwks`,
        }),
      )
      return
    }

    if (req.url === '/oauth2/jwks') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ keys: [publicJwk] }))
      return
    }

    if (req.url === '/ready') {
      sendTestJson(res, {
        status: 'ready',
        service: 'ai-service',
        dependencies: { sessions: { status: 'ready' } },
      })
      return
    }

    if (req.url === `/api/v1/machines/${machineId}/context`) {
      sendTestJson(res, {
        machine: { id: machineId },
        manual: null,
        documents: [],
        telemetry: null,
        contract: serviceContract(machineId),
      })
      return
    }

    if (req.url === `/api/v1/machines/${machineId}/contract`) {
      sendTestJson(res, serviceContract(machineId))
      return
    }

    if (req.url === `/api/v1/machines/${machineId}/warranty`) {
      sendTestJson(res, serviceContract(machineId))
      return
    }

    if (req.url === `/api/v1/machines/${machineId}/orders`) {
      sendTestJson(res, [{
        orderNumber: 'SO-100',
        machineId,
        type: 'machine',
        status: 'delivered',
        openedAt: '2019-01-01',
        expectedAt: '2019-03-01',
        summary: 'AROL Euro VP delivery',
      }])
      return
    }

    if (req.url === `/api/v1/machines/${machineId}/service-history`) {
      sendTestJson(res, [{
        id: 'service-1',
        machineId,
        serviceDate: '2026-01-15',
        type: 'preventive-maintenance',
        status: 'completed',
        summary: 'Annual inspection',
        technician: 'AROL Service',
        orderNumber: 'SO-100',
      }])
      return
    }

    if (req.url === '/api/v1/manuals/index/status') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify(manualIndexStatus()))
      return
    }

    if (req.url === '/api/v1/manuals/search') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify(manualSearchResponse()))
      return
    }

    if (req.url === '/api/v1/chat/sessions') {
      res.writeHead(201, {
        'Content-Type': 'application/json',
        'Idempotency-Replayed': String(idempotencyWasReplayed),
      })
      res.end(JSON.stringify(chatSession(body?.machineId, req.headers['x-arol-subject'])))
      return
    }

    if (req.url?.startsWith('/api/v1/chat/sessions/')) {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify(chatSession(machineId, req.headers['x-arol-subject'])))
      return
    }

    if (req.url === '/api/v1/chat/stream') {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Idempotency-Replayed': String(idempotencyWasReplayed),
      })
      res.write('event: trace\ndata: {"agentTrace":["supervisor"],"intents":["documentation"],"routingReason":"test"}\n\n')
      await new Promise((resolve) => setTimeout(resolve, 40))
      res.write('event: progress\ndata: {"stage":"doc-agent","status":"completed"}\n\n')
      await new Promise((resolve) => setTimeout(resolve, 40))
      res.write('event: progress\ndata: {"stage":"answer","status":"completed"}\n\n')
      await new Promise((resolve) => setTimeout(resolve, 40))
      res.write('event: token\ndata: {"delta":"ok "}\n\n')
      res.end(`event: done\ndata: ${JSON.stringify(chatResponse())}\n\n`)
      return
    }

    res.writeHead(200, {
      'Content-Type': 'application/json',
      'Idempotency-Replayed': String(idempotencyWasReplayed),
    })
    res.end(JSON.stringify(body?.message?.includes('bypass') ? reviewResponse() : chatResponse()))
  })

  await listen(aiServer, 0)
  t.after(() => aiServer.close())

  const telemetryServer = http.createServer((req, res) => {
    observedTelemetryRequests.push({ path: req.url, headers: req.headers })
    if (req.url === '/health') {
      sendTestJson(res, { status: 'ok', service: 'telemetry-mcp' })
      return
    }

    if (req.url === `/api/v1/machines/${machineId}/telemetry/latest`) {
      sendTestJson(res, telemetrySample(machineId))
      return
    }

    if (req.url === `/api/v1/machines/${machineId}/telemetry/history?limit=2`) {
      sendTestJson(res, [
        telemetrySample(machineId),
        {
          ...telemetrySample(machineId),
          timestamp: '2030-01-02T03:03:05.678Z',
          activeAlarm: 'NO_ALARM',
          health: 'healthy',
          ageSeconds: 60,
        },
      ])
      return
    }

    if (req.url === `/api/v1/machines/${machineId}/alarms`) {
      sendTestJson(res, [
        {
          machineId,
          code: 'TORQUE_HIGH',
          severity: 'warning',
          status: 'active',
          startedAt: '2026-05-23T10:10:00.000Z',
          clearedAt: null,
          description: 'Application torque exceeded the simulated operating threshold.',
          source: 'iot-simulator',
        },
      ])
      return
    }

    sendTestJson(res, { error: 'Not found.' }, 404)
  })

  await listen(telemetryServer, 0)
  t.after(() => telemetryServer.close())

  const gateway = await startGateway({
    AI_SERVICE_URL: `http://127.0.0.1:${aiServer.address().port}`,
    TELEMETRY_SERVICE_URL: `http://127.0.0.1:${telemetryServer.address().port}`,
    OPERATIONS_DATA_DIR: operationsDir,
    GATEWAY_AUTH_MODE: 'oidc',
    AI_SERVICE_SHARED_SECRET: 'gateway-test-secret',
    AI_SERVICE_SHARED_SECRETS: 'gateway-current-secret,gateway-test-secret',
    OIDC_ISSUER: issuer,
    OIDC_AUDIENCE: audience,
    OIDC_CLIENT_ID: 'arol-q2-web',
    OIDC_REDIRECT_URI: 'http://localhost:5173/api/v1/auth/callback',
    GATEWAY_CSRF_SECRET: 'gateway-test-csrf-secret',
    OIDC_JWKS_URI: `http://127.0.0.1:${aiServer.address().port}/oauth2/jwks`,
    GATEWAY_MACHINE_IDS: machineId,
    RATE_LIMIT_MAX: '1000',
    AI_SERVICE_STREAM_CONNECT_TIMEOUT_MS: '1000',
    AI_SERVICE_STREAM_IDLE_TIMEOUT_MS: '70',
  })
  t.after(() => gateway.stop())
  t.after(() => rm(operationsDir, { recursive: true, force: true }))

  const baseUrl = `http://127.0.0.1:${gateway.port}`
  const unauthorized = await fetch(`${baseUrl}/api/v1/machines/${machineId}/context`)
  assert.equal(unauthorized.status, 401)
  const readiness = await getJson(`${baseUrl}/ready`)
  assert.equal(readiness.status, 'ready')
  assert.equal(readiness.dependencies.authentication.status, 'ready')
  assert.equal(readiness.dependencies.authentication.mode, 'oidc')
  assert.equal(readiness.dependencies.aiService.status, 'ready')
  assert.equal(readiness.dependencies.telemetry.status, 'ready')

  const preflight = await fetch(`${baseUrl}/api/v1/machines/${machineId}/context`, {
    method: 'OPTIONS',
    headers: {
      Origin: 'http://localhost:5173',
      'Access-Control-Request-Headers': 'authorization',
    },
  })
  assert.equal(preflight.status, 204)
  assert.equal(preflight.headers.get('access-control-allow-credentials'), 'true')
  assert.match(preflight.headers.get('access-control-allow-headers') ?? '', /x-csrf-token/i)
  assert.doesNotMatch(preflight.headers.get('access-control-allow-headers') ?? '', /authorization/i)

  const token = signJwt(
    {
      iss: issuer,
      aud: audience,
      sub: 'operator-1',
      machine_ids: [machineId],
      roles: ['operator', 'arol-support'],
      company_id: 'CMP-003',
      visibility: 'full',
      exp: Math.floor(Date.now() / 1000) + 300,
    },
    privateKey,
    publicJwk.kid,
  )
  const headers = { Cookie: `${authCookieName}=${token}`, 'X-Request-Id': 'gateway-test-request' }
  const operatorToken = signJwt(
    {
      iss: issuer,
      aud: audience,
      sub: 'operator-only',
      machine_ids: [machineId],
      roles: ['operator'],
      company_id: 'CMP-003',
      visibility: 'full',
      exp: Math.floor(Date.now() / 1000) + 300,
    },
    privateKey,
    publicJwk.kid,
  )
  const operatorHeaders = { Authorization: `Bearer ${operatorToken}` }
  const otherMachineToken = signJwt(
    {
      iss: issuer,
      aud: audience,
      sub: 'other-machine-operator',
      machine_ids: ['MCH-0006'],
      roles: ['operator'],
      company_id: 'CMP-004',
      visibility: 'full',
      exp: Math.floor(Date.now() / 1000) + 300,
    },
    privateKey,
    publicJwk.kid,
  )

  const authSession = await getJson(`${baseUrl}/api/v1/auth/session`, { headers })
  assert.equal(authSession.mode, 'oidc')
  assert.equal(authSession.subject, 'operator-1')
  assert.deepEqual(authSession.roles, ['operator', 'arol-support'])
  assert.deepEqual(authSession.machineIds, [machineId])
  assert.equal(authSession.hasMachineRestriction, true)
  assert.equal(authSession.token, undefined)
  assert.match(authSession.csrfToken, /^csrf-v1\./)
  const missingCsrf = await fetch(`${baseUrl}/api/v1/manuals/search`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...headers,
    },
    body: JSON.stringify({
      machineId,
      query: 'torque alarm',
    }),
  })
  assert.equal(missingCsrf.status, 403)
  headers['X-CSRF-Token'] = authSession.csrfToken

  const machines = await getJson(`${baseUrl}/api/v1/machines`, { headers })
  assert.equal(machines.machines[0].id, machineId)

  const context = await getJson(`${baseUrl}/api/v1/machines/${machineId}/context`, { headers })
  assert.equal(context.machine.id, machineId)
  assert.equal(context.telemetry.source, 'iot-simulator')
  assert.equal(context.telemetry.quality, 'fresh')
  assert.equal(context.telemetry.ageSeconds, 0)
  assert.equal(context.telemetry.staleAfterSeconds, 900)
  assert.deepEqual(context.telemetry.missingFields, [])
  // Service standing, not a warranty: the dataset records delivery, cost and
  // maintenance, and says in words that coverage is not in it.
  assert.equal(context.contract.deliveryDate, '2021-05-14')
  assert.match(context.contract.coverageNote, /no warranty or SLA records/)
  assert.equal(context.manual.serialNumber, '17478')
  assert.equal(context.manual.url, '/manuals/17478_manual_EN.pdf')
  assert.equal(
    observedTelemetryRequests.find((request) => request.path?.endsWith('/telemetry/latest'))?.headers[
      'x-request-id'
    ],
    'gateway-test-request',
  )

  const contextHeaders = await fetch(`${baseUrl}/api/v1/machines/${machineId}/context`, { headers })
  assert.equal(contextHeaders.headers.get('x-content-type-options'), 'nosniff')
  assert.equal(contextHeaders.headers.get('x-frame-options'), 'DENY')
  assert.equal(contextHeaders.headers.get('referrer-policy'), 'no-referrer')
  assert.match(contextHeaders.headers.get('content-security-policy') ?? '', /frame-ancestors 'none'/)

  const bearerContext = await getJson(`${baseUrl}/api/v1/machines/${machineId}/context`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  assert.equal(bearerContext.machine.id, machineId)

  const forbidden = await fetch(`${baseUrl}/api/v1/machines/MCH-0006/context`, { headers })
  assert.equal(forbidden.status, 403)

  const unknownMachine = await fetch(`${baseUrl}/api/v1/machines/other-machine/context`, { headers })
  assert.equal(unknownMachine.status, 404)

  for (const sensitivePath of [
    '/api/v1/audit/events',
    '/api/v1/alerts/events',
  ]) {
    const sensitiveResponse = await fetch(`${baseUrl}${sensitivePath}`, { headers: operatorHeaders })
    assert.equal(sensitiveResponse.status, 403)
  }

  const manualPdf = await fetch(
    `${baseUrl}/manuals/17478_manual_EN.pdf`,
    { headers: operatorHeaders },
  )
  assert.equal(manualPdf.status, 200)
  assert.equal(manualPdf.headers.get('content-type'), 'application/pdf')
  assert.equal(manualPdf.headers.get('x-frame-options'), null)
  assert.match(
    manualPdf.headers.get('content-security-policy') ?? '',
    /frame-ancestors 'self' http:\/\/localhost:5173/,
  )
  assert.match(manualPdf.headers.get('cache-control') ?? '', /private/)
  assert.equal(manualPdf.headers.get('accept-ranges'), 'bytes')
  assert.ok(Number(manualPdf.headers.get('content-length')) > 0)
  assert.match(manualPdf.headers.get('etag') ?? '', /^"[a-f0-9]+-[a-f0-9]+"$/)

  const manualUrl = `${baseUrl}/manuals/17478_manual_EN.pdf`
  const manualHead = await fetch(manualUrl, { method: 'HEAD', headers: operatorHeaders })
  assert.equal(manualHead.status, 200)
  assert.equal(manualHead.headers.get('content-length'), manualPdf.headers.get('content-length'))
  assert.equal((await manualHead.arrayBuffer()).byteLength, 0)

  const manualRange = await fetch(manualUrl, {
    headers: { ...operatorHeaders, Range: 'bytes=0-99' },
  })
  assert.equal(manualRange.status, 206)
  assert.equal(manualRange.headers.get('content-range'), `bytes 0-99/${manualPdf.headers.get('content-length')}`)
  assert.equal(manualRange.headers.get('content-length'), '100')
  assert.equal(Buffer.from(await manualRange.arrayBuffer()).subarray(0, 4).toString(), '%PDF')

  const unchangedManual = await fetch(manualUrl, {
    headers: { ...operatorHeaders, 'If-None-Match': manualPdf.headers.get('etag') },
  })
  assert.equal(unchangedManual.status, 304)

  const invalidManualRange = await fetch(manualUrl, {
    headers: { ...operatorHeaders, Range: 'bytes=999999999-' },
  })
  assert.equal(invalidManualRange.status, 416)
  assert.equal(
    invalidManualRange.headers.get('content-range'),
    `bytes */${manualPdf.headers.get('content-length')}`,
  )

  // Manuals in the fleet dataset are machine-specific, so an identity scoped to
  // one machine must not reach another machine's manual even with a valid token.
  const otherMachineManual = await fetch(`${baseUrl}/manuals/A2064_manual_EN.pdf`, {
    headers: operatorHeaders,
  })
  assert.equal(otherMachineManual.status, 403)
  const forbiddenManual = await fetch(
    `${baseUrl}/manuals/17478_manual_EN.pdf`,
    { headers: { Authorization: `Bearer ${otherMachineToken}` } },
  )
  assert.equal(forbiddenManual.status, 403)
  const unregisteredManual = await fetch(
    `${baseUrl}/manuals/AROL_GENERAL_CATALOGUE_11.0_EN_20230215.pdf`,
    { headers: operatorHeaders },
  )
  assert.equal(unregisteredManual.status, 404)

  const history = await getJson(`${baseUrl}/api/v1/machines/${machineId}/telemetry/history?limit=2`, {
    headers,
  })
  assert.equal(history.length, 2)
  assert.equal(history[0].activeAlarm, 'TORQUE_HIGH')
  assert.equal(history[0].quality, 'fresh')
  assert.equal(history[1].quality, 'fresh')
  assert.equal(history[1].ageSeconds, 60)

  const alarms = await getJson(`${baseUrl}/api/v1/machines/${machineId}/alarms`, { headers })
  assert.equal(alarms[0].code, 'TORQUE_HIGH')

  const contract = await getJson(`${baseUrl}/api/v1/machines/${machineId}/contract`, { headers })
  assert.equal(contract.deliveryDate, '2021-05-14')
  assert.equal(contract.openTicketCount, 2)

  // /warranty is kept as a route because that is the question people ask, but
  // it answers with the service standing and says the dataset holds no
  // coverage record, rather than returning a blank status.
  const warranty = await getJson(`${baseUrl}/api/v1/machines/${machineId}/warranty`, { headers })
  assert.equal(warranty.machineId, machineId)
  assert.match(warranty.coverageNote, /no warranty or SLA records/)

  // Orders come from the fleet dataset now, and their content is taken from the
  // approved revision of the quote they were raised against; the order lines
  // themselves carry only fulfilment.
  const orders = await getJson(`${baseUrl}/api/v1/machines/${machineId}/orders`, { headers })
  assert.ok(Array.isArray(orders))
  for (const order of orders) {
    assert.match(order.orderId, /^ORD-/)
    assert.equal(order.companyId, 'CMP-003')
    assert.ok(order.lines.length > 0, 'order content should come from the approved revision')
    assert.ok(order.fulfillment.length > 0, 'order lines track fulfilment')
  }

  const serviceHistory = await getJson(`${baseUrl}/api/v1/machines/${machineId}/service-history`, {
    headers,
  })
  assert.equal(serviceHistory[0].id, 'service-1')

  const manualStatus = await getJson(`${baseUrl}/api/v1/manuals/index/status`, { headers })
  assert.equal(manualStatus.status, 'indexed')
  assert.equal(manualStatus.totalIndexedChunks, 3)

  const manualSearch = await postJson(
    `${baseUrl}/api/v1/manuals/search`,
    {
      machineId,
      query: 'torque alarm',
      limit: 2,
    },
    { headers },
  )
  assert.equal(manualSearch.evidence[0].sourceUri, '/manuals/Original%20manual%202019%20Arol%20Euro%20VIP.pdf')

  const bearerManualSearch = await postJson(
    `${baseUrl}/api/v1/manuals/search`,
    {
      machineId,
      query: 'torque alarm',
      limit: 2,
    },
    { headers: { Authorization: `Bearer ${token}` } },
  )
  assert.equal(bearerManualSearch.evidence[0].source, 'manual')

  const createdSession = await postJson(
    `${baseUrl}/api/v1/chat/sessions`,
    { machineId },
    { headers: { ...headers, 'Idempotency-Key': 'session-create-001' } },
  )
  assert.equal(createdSession.machineId, machineId)
  assert.equal(createdSession.ownerSubject, 'operator-1')
  assert.equal(
    observedAiRequests.findLast((request) => request.path === '/api/v1/chat/sessions').headers[
      'idempotency-key'
    ],
    'session-create-001',
  )

  const loadedSession = await getJson(`${baseUrl}/api/v1/chat/sessions/${createdSession.sessionId}`, {
    headers,
  })
  assert.equal(loadedSession.sessionId, createdSession.sessionId)

  const chat = await postJson(
    `${baseUrl}/api/v1/chat/messages`,
    {
      sessionId: 'gateway-test-session',
      machineId,
      message: 'What should I check for a torque alarm?',
      attachments: [{
        name: 'operator-note.txt',
        type: 'text/plain',
        contentBase64: Buffer.from('Torque increased after the last adjustment.').toString('base64'),
      }],
    },
    { headers: { ...headers, 'Idempotency-Key': 'chat-turn-001' } },
  )
  assert.equal(chat.message.content, 'AI response')
  assert.equal(observedAiRequests.at(-1).requestId, 'gateway-test-request')
  const observedChat = observedAiRequests.findLast((request) => request.path === '/api/v1/chat/complete')
  assert.equal(observedChat.headers['x-arol-subject'], 'operator-1')
  assert.equal(observedChat.headers['x-arol-roles'], 'operator,arol-support')
  assert.equal(observedChat.headers['x-arol-machine-ids'], machineId)
  assert.equal(observedChat.headers['x-arol-internal-secret'], 'gateway-current-secret')
  assert.equal(observedChat.headers['idempotency-key'], 'chat-turn-001')
  assert.equal(JSON.parse(observedChat.body).messages, undefined)
  assert.equal(JSON.parse(observedChat.body).attachments[0].size, 43)
  assert.match(JSON.parse(observedChat.body).attachments[0].id, /^att-[a-f0-9]{24}$/)

  const replayedChat = await fetch(`${baseUrl}/api/v1/chat/messages`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...headers,
      'Idempotency-Key': 'chat-turn-001',
    },
    body: JSON.stringify({
      sessionId: 'gateway-test-session',
      machineId,
      message: 'What should I check for a torque alarm?',
      attachments: [{
        name: 'operator-note.txt',
        type: 'text/plain',
        contentBase64: Buffer.from('Torque increased after the last adjustment.').toString('base64'),
      }],
    }),
  })
  assert.equal(replayedChat.status, 200)
  assert.equal(replayedChat.headers.get('idempotency-replayed'), 'true')
  assert.equal((await replayedChat.json()).message.content, 'AI response')

  const conflictingIdempotency = await fetch(`${baseUrl}/api/v1/chat/messages`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...headers,
      'Idempotency-Key': 'header-key',
    },
    body: JSON.stringify({
      sessionId: 'gateway-test-session',
      machineId,
      message: 'conflicting key',
      idempotencyKey: 'body-key',
    }),
  })
  assert.equal(conflictingIdempotency.status, 400)

  const unsafeAttachment = await fetch(`${baseUrl}/api/v1/chat/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify({
      sessionId: 'gateway-test-session',
      machineId,
      message: 'unsafe attachment',
      attachments: [{
        name: '../secret.txt',
        type: 'text/plain',
        contentBase64: Buffer.from('nope').toString('base64'),
      }],
    }),
  })
  assert.equal(unsafeAttachment.status, 400)

  const spoofedAttachment = await fetch(`${baseUrl}/api/v1/chat/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify({
      sessionId: 'gateway-test-session',
      machineId,
      message: 'spoofed attachment',
      attachments: [{
        name: 'not-really-a-pdf.pdf',
        type: 'application/pdf',
        contentBase64: Buffer.from('plain text').toString('base64'),
      }],
    }),
  })
  assert.equal(spoofedAttachment.status, 400)

  const boundaryAttachmentName = `${'a'.repeat(156)}.txt`
  assert.equal(boundaryAttachmentName.length, 160)
  const boundaryAttachment = await fetch(`${baseUrl}/api/v1/chat/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify({
      sessionId: 'gateway-attachment-boundary',
      machineId,
      message: 'filename boundary',
      attachments: [{
        name: boundaryAttachmentName,
        type: 'text/plain',
        contentBase64: Buffer.from('valid boundary').toString('base64'),
      }],
    }),
  })
  assert.equal(boundaryAttachment.status, 200)

  const oversizedAttachmentName = await fetch(`${baseUrl}/api/v1/chat/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify({
      sessionId: 'gateway-attachment-over-boundary',
      machineId,
      message: 'filename over boundary',
      attachments: [{
        name: `${'a'.repeat(157)}.txt`,
        type: 'text/plain',
        contentBase64: Buffer.from('invalid boundary').toString('base64'),
      }],
    }),
  })
  assert.equal(oversizedAttachmentName.status, 400)
  assert.match((await oversizedAttachmentName.json()).error, /160 characters/)

  const emptyIdempotencyKey = await fetch(`${baseUrl}/api/v1/chat/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify({ machineId, idempotencyKey: '' }),
  })
  assert.equal(emptyIdempotencyKey.status, 400)

  const reviewChat = await postJson(
    `${baseUrl}/api/v1/chat/messages`,
    {
      sessionId: 'gateway-review-session',
      machineId,
      message: 'Can I bypass guard and keep running with alarm?',
    },
    { headers },
  )
  assert.equal(reviewChat.reviewRequired, true)
  assert.match(reviewChat.reviewReasons.join(','), /safety/)

  const repeatedReviewChat = await postJson(
    `${baseUrl}/api/v1/chat/messages`,
    {
      sessionId: 'gateway-review-session-repeat',
      machineId,
      message: 'Can I bypass guard and keep running with alarm?',
    },
    { headers },
  )
  assert.equal(repeatedReviewChat.reviewRequired, true)

  for (const message of ['First distinct request-id turn.', 'Second distinct request-id turn.']) {
    await postJson(
      `${baseUrl}/api/v1/chat/messages`,
      {
        sessionId: 'request-id-collision-session',
        machineId,
        message,
      },
      { headers },
    )
  }

  const auditEvents = await getJson(`${baseUrl}/api/v1/audit/events?limit=20`, { headers })
  assert.ok(auditEvents.events.some((event) => event.type === 'chat.completed'))
  assert.equal(
    auditEvents.events.filter(
      (event) => event.type === 'chat.completed' && event.sessionId === 'gateway-test-session',
    ).length,
    1,
  )
  assert.equal(
    auditEvents.events.filter(
      (event) => event.type === 'chat.completed' && event.sessionId === 'request-id-collision-session',
    ).length,
    2,
  )
  const metrics = await fetch(`${baseUrl}/metrics`, { headers })
  assert.equal(metrics.status, 200)
  const metricsText = await metrics.text()
  assert.match(metricsText, /arol_gateway_http_requests_total/)
  assert.match(metricsText, /arol_gateway_http_request_duration_ms_bucket\{.*le="50"\}/)
  assert.match(metricsText, /arol_gateway_http_request_duration_ms_count/)

  const legacyGetStream = await fetch(
    `${baseUrl}/api/v1/chat/stream?sessionId=gateway-stream&machineId=${machineId}&message=hello`,
    { headers },
  )
  assert.equal(legacyGetStream.status, 405)
  assert.equal(legacyGetStream.headers.get('allow'), 'POST')

  const postStream = await fetch(`${baseUrl}/api/v1/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...headers,
      'Idempotency-Key': 'stream-turn-001',
    },
    body: JSON.stringify({
      sessionId: 'gateway-post-stream',
      machineId,
      message: 'hello from post',
      idempotencyKey: 'stream-turn-001',
    }),
  })
  assert.equal(postStream.status, 200)
  assert.equal(postStream.headers.get('idempotency-key'), 'stream-turn-001')
  const postStreamText = await postStream.text()
  assert.match(postStreamText, /event: trace/)
  assert.match(postStreamText, /event: token/)
  assert.match(postStreamText, /event: done/)
  assert.equal(JSON.parse(observedAiRequests.at(-1).body).message, 'hello from post')
  assert.equal(observedAiRequests.at(-1).headers['idempotency-key'], 'stream-turn-001')
  const streamMetrics = await (await fetch(`${baseUrl}/metrics`, { headers })).text()
  assert.match(streamMetrics, /arol_gateway_stream_first_token_duration_ms_bucket/)
  assert.match(
    streamMetrics,
    /arol_gateway_stream_first_token_duration_ms_bucket\{stream="chat",le="50"\} 0/,
  )

  const wrongAudienceToken = signJwt(
    {
      iss: issuer,
      aud: 'wrong-audience',
      sub: 'operator-1',
      machine_ids: [machineId],
      company_id: 'CMP-003',
      visibility: 'full',
      exp: Math.floor(Date.now() / 1000) + 300,
    },
    privateKey,
    publicJwk.kid,
  )
  const invalidAudience = await fetch(`${baseUrl}/api/v1/machines/${machineId}/context`, {
    headers: { Cookie: `${authCookieName}=${wrongAudienceToken}` },
  })
  assert.equal(invalidAudience.status, 401)
})

test('gateway rate limits non-health requests', async (t) => {
  const gateway = await startGateway({
    RATE_LIMIT_MAX: '1',
    RATE_LIMIT_WINDOW_MS: '60000',
    GATEWAY_TRUST_PROXY_HOPS: '0',
  })
  t.after(() => gateway.stop())

  const baseUrl = `http://127.0.0.1:${gateway.port}`
  assert.equal(
    (
      await fetch(`${baseUrl}/api/v1/machines/MCH-0004`, {
        headers: { 'X-Forwarded-For': '198.51.100.10' },
      })
    ).status,
    200,
  )
  assert.equal(
    (
      await fetch(`${baseUrl}/api/v1/machines/MCH-0004/context`, {
        headers: { 'X-Forwarded-For': '203.0.113.20' },
      })
    ).status,
    429,
  )
  assert.equal((await fetch(`${baseUrl}/health`)).status, 200)
})

test('gateway readiness fails closed when a required dependency is unavailable', async (t) => {
  const unavailablePort = await openPort()
  const gateway = await startGateway({
    AI_SERVICE_URL: `http://127.0.0.1:${unavailablePort}`,
    GATEWAY_AUTH_MODE: 'oidc',
    READINESS_TIMEOUT_MS: '100',
    RATE_LIMIT_MAX: '1000',
  })
  t.after(() => gateway.stop())

  const baseUrl = `http://127.0.0.1:${gateway.port}`
  assert.equal((await fetch(`${baseUrl}/health`)).status, 200)
  const response = await fetch(`${baseUrl}/ready`)
  assert.equal(response.status, 503)
  const payload = await response.json()
  assert.equal(payload.status, 'not_ready')
  assert.equal(payload.dependencies.aiService.status, 'unavailable')
  assert.equal(payload.dependencies.authentication.status, 'unavailable')
  assert.deepEqual(
    payload.dependencies.authentication.missing,
    ['GATEWAY_CSRF_SECRET', 'OIDC_ISSUER', 'OIDC_AUDIENCE', 'OIDC_CLIENT_ID', 'OIDC_REDIRECT_URI'],
  )
  assert.ok(payload.unavailable.includes('aiService'))
  assert.ok(payload.unavailable.includes('authentication'))
})

test('gateway bounds stalled OIDC discovery requests', async (t) => {
  const stalledProvider = http.createServer(() => {
    // Intentionally leave the response open; the gateway must enforce its own
    // identity-provider timeout and release the request.
  })
  await listen(stalledProvider, 0)
  t.after(() => stalledProvider.close())

  const gateway = await startGateway({
    GATEWAY_AUTH_MODE: 'oidc',
    OIDC_ISSUER: `http://127.0.0.1:${stalledProvider.address().port}`,
    OIDC_AUDIENCE: 'arol-q2-api',
    OIDC_CLIENT_ID: 'arol-q2-client',
    OIDC_REDIRECT_URI: 'http://127.0.0.1/callback',
    OIDC_REQUEST_TIMEOUT_MS: '100',
    RATE_LIMIT_MAX: '1000',
  })
  t.after(() => gateway.stop())

  const startedAt = Date.now()
  const response = await fetch(`http://127.0.0.1:${gateway.port}/api/v1/auth/login`, {
    redirect: 'manual',
  })
  assert.equal(response.status, 503)
  assert.ok(Date.now() - startedAt < 2000)
  assert.equal((await fetch(`http://127.0.0.1:${gateway.port}/health`)).status, 200)
})

test('gateway withholds stream completion when operational persistence fails', async (t) => {
  const aiServer = http.createServer((req, res) => {
    if (req.url === '/api/v1/chat/stream') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' })
      res.write('event: token\ndata: {"delta":"partial "}\n\n')
      res.end(`event: done\ndata: ${JSON.stringify(chatResponse())}\n\n`)
      return
    }
    sendTestJson(res, { status: 'ready' })
  })
  await listen(aiServer, 0)
  t.after(() => aiServer.close())

  const operationsDir = await mkdtemp(path.join(os.tmpdir(), 'arol-q2-broken-ops-'))
  const databasePath = path.join(operationsDir, 'operations.sqlite')

  const gateway = await startGateway({
    AI_SERVICE_URL: `http://127.0.0.1:${aiServer.address().port}`,
    OPERATIONS_DATA_DIR: operationsDir,
    OPERATIONS_DB_PATH: databasePath,
    RATE_LIMIT_MAX: '1000',
  })
  t.after(() => gateway.stop())

  // The gateway boots with a working store, then loses it: a second writer
  // holds the database, so the audit commit at the end of the stream fails the
  // way a full disk or a stalled peer would. The point is what the operator
  // sees when that happens, not how the write broke.
  const blocker = new DatabaseSync(databasePath)
  t.after(() => blocker.close())
  // Removal comes last: both writers must let the database go first.
  t.after(() => rm(operationsDir, { recursive: true, force: true }))
  blocker.exec('BEGIN IMMEDIATE')
  blocker.prepare('INSERT INTO audit_events (id, timestamp, machine_id, payload) VALUES (?, ?, ?, ?)')
    .run('lock-holder', new Date().toISOString(), null, '{}')

  const response = await fetch(`http://127.0.0.1:${gateway.port}/api/v1/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': 'ops-failure-stream-001',
    },
    body: JSON.stringify({
      sessionId: 'ops-failure-session',
      machineId: 'MCH-0004',
      message: 'Test safe finalization.',
      idempotencyKey: 'ops-failure-stream-001',
    }),
  })
  assert.equal(response.status, 200)
  const body = await response.text()
  assert.match(body, /event: token/)
  assert.match(body, /event: error/)
  assert.match(body, /operational_commit_failed/)
  assert.doesNotMatch(body, /event: done/)
  assert.equal((await fetch(`http://127.0.0.1:${gateway.port}/health`)).status, 200)
})

test('gateway strips query secrets from logs and alerts and ignores malformed Host values', async (t) => {
  const operationsDir = await mkdtemp(path.join(os.tmpdir(), 'arol-q2-redaction-ops-'))
  const unavailablePort = await openPort()
  const gateway = await startGateway({
    AI_SERVICE_URL: `http://127.0.0.1:${unavailablePort}`,
    OPERATIONS_DATA_DIR: operationsDir,
    RATE_LIMIT_MAX: '1000',
  })
  t.after(() => gateway.stop())
  t.after(() => rm(operationsDir, { recursive: true, force: true }))

  const baseUrl = `http://127.0.0.1:${gateway.port}`
  const oauthCode = 'oauth-code-must-not-be-logged'
  const oauthState = 'oauth-state-must-not-be-logged'
  const failed = await fetch(
    `${baseUrl}/api/v1/manuals/index/status?code=${oauthCode}&state=${oauthState}`,
  )
  assert.equal(failed.status, 502)

  let alerts
  await waitUntil(async () => {
    alerts = await getJson(`${baseUrl}/api/v1/alerts/events?limit=10`)
    return alerts.events.length > 0
  })
  const serializedAlerts = JSON.stringify(alerts)
  assert.doesNotMatch(serializedAlerts, new RegExp(oauthCode))
  assert.doesNotMatch(serializedAlerts, new RegExp(oauthState))
  assert.equal(alerts.events[0].metadata.path, '/api/v1/manuals/index/status')

  const rawResponse = await rawHttpRequest(
    gateway.port,
    'GET /health?code=host-secret HTTP/1.1\r\nHost: [malformed-host\r\nConnection: close\r\n\r\n',
  )
  assert.match(rawResponse, /^HTTP\/1\.1 200/)

  await waitUntil(() => gateway.output().includes('/health'))
  assert.doesNotMatch(gateway.output(), /oauth-code-must-not-be-logged/)
  assert.doesNotMatch(gateway.output(), /oauth-state-must-not-be-logged/)
  assert.doesNotMatch(gateway.output(), /host-secret/)
})

test('an unauthenticated refusal carries a branchable code, not a token diagnosis', async (t) => {
  const operationsDir = await mkdtemp(path.join(os.tmpdir(), 'arol-q2-auth-code-'))
  const gateway = await startGateway({
    GATEWAY_AUTH_MODE: 'demo',
    OPERATIONS_DATA_DIR: operationsDir,
  })
  t.after(() => gateway.stop())
  t.after(() => rm(operationsDir, { recursive: true, force: true }))

  const response = await fetch(`http://127.0.0.1:${gateway.port}/api/v1/machines`)
  assert.equal(response.status, 401)

  const body = await response.json()
  // The client needs something to branch on. Without a code it has only the
  // prose, and printing the prose is what put "JWT is not active yet" and an
  // internal hostname in front of a plant operator.
  assert.equal(body.code, 'session_expired')

  // Whatever the wording becomes, it must not name the mechanism.
  const serialized = JSON.stringify(body)
  for (const leak of ['JWT', 'CSRF', 'OIDC', 'cookie', 'Bearer', 'kid', 'JWKS']) {
    assert.ok(
      !serialized.includes(leak),
      `refusal body leaked ${leak} to the browser: ${serialized}`,
    )
  }
})

test('a QR code carrying the serial number reaches the assistant, not a refusal', async (t) => {
  // requirements/README.md, QR Codes: the code may encode either the machineId
  // or the serialNumber, and https://<platform>/machines/17203 is its own
  // example. The gateway resolves either; the AI service knows machineId only,
  // so what gets forwarded has to be the resolved one.
  const machineId = 'MCH-0002'
  const serialNumber = '17203'
  const seen = []
  const contextPaths = []
  const aiServer = http.createServer((req, res) => {
    let raw = ''
    req.on('data', (chunk) => { raw += chunk })
    req.on('end', () => {
      if (req.url === '/api/v1/chat/sessions') {
        seen.push(JSON.parse(raw || '{}').machineId)
        sendTestJson(res, { sessionId: 'session-1', machineId })
        return
      }
      if (req.url.endsWith('/context')) {
        contextPaths.push(req.url)
        if (req.url !== `/api/v1/machines/${machineId}/context`) {
          res.writeHead(403)
          res.end('{}')
        } else {
          sendTestJson(res, { contract: null })
        }
        return
      }
      sendTestJson(res, { status: 'ready' })
    })
  })
  await listen(aiServer, 0)
  t.after(() => aiServer.close())

  const operationsDir = await mkdtemp(path.join(os.tmpdir(), 'arol-q2-qr-'))
  const gateway = await startGateway({
    AI_SERVICE_URL: `http://127.0.0.1:${aiServer.address().port}`,
    OPERATIONS_DATA_DIR: operationsDir,
    GATEWAY_AUTH_MODE: 'off',
  })
  t.after(() => gateway.stop())
  t.after(() => rm(operationsDir, { recursive: true, force: true }))

  const response = await fetch(`http://127.0.0.1:${gateway.port}/api/v1/chat/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ machineId: serialNumber }),
  })

  assert.equal(response.status, 201)
  assert.deepEqual(seen, [machineId], 'the serial must be resolved before it is forwarded')
  const contextResponse = await fetch(`http://127.0.0.1:${gateway.port}/api/v1/machines/${serialNumber}/context`)
  assert.equal(contextResponse.status, 200)
  assert.deepEqual(contextPaths, [`/api/v1/machines/${machineId}/context`])
})

test('gateway demo auth sets an HttpOnly JWT cookie without exposing the token', async (t) => {
  const machineId = 'MCH-0004'
  const aiServer = http.createServer((req, res) => {
    if (req.url === `/api/v1/machines/${machineId}/context`) {
      sendTestJson(res, {
        machine: { id: machineId },
        manual: null,
        documents: [],
        telemetry: null,
        contract: serviceContract(machineId),
      })
      return
    }

    sendTestJson(res, { error: 'Not found.' }, 404)
  })
  await listen(aiServer, 0)
  t.after(() => aiServer.close())

  const gateway = await startGateway({
    AI_SERVICE_URL: `http://127.0.0.1:${aiServer.address().port}`,
    GATEWAY_AUTH_MODE: 'demo',
    GATEWAY_AUTH_COOKIE_NAME: 'arol_q2_demo_access',
    GATEWAY_AUTH_COOKIE_SECURE: 'false',
    GATEWAY_HSTS: 'true',
    GATEWAY_AUTH_DEMO_USER_ID: 'USR-011',
    GATEWAY_AUTH_DEMO_ROLES: 'operator',
    GATEWAY_AUTH_DEMO_MACHINE_IDS: machineId,
    GATEWAY_MACHINE_IDS: machineId,
    RATE_LIMIT_MAX: '1000',
  })
  t.after(() => gateway.stop())

  const baseUrl = `http://127.0.0.1:${gateway.port}`
  assert.equal((await fetch(`${baseUrl}/api/v1/machines/${machineId}/context`)).status, 401)

  const malformedCookieHeaders = { Cookie: 'arol_q2_demo_access=%E0%A4%A' }
  const malformedSession = await fetch(`${baseUrl}/api/v1/auth/session`, {
    headers: malformedCookieHeaders,
  })
  assert.equal(malformedSession.status, 200)
  assert.equal((await malformedSession.json()).authenticated, false)
  assert.equal(
    (
      await fetch(`${baseUrl}/api/v1/machines/${machineId}/context`, {
        headers: malformedCookieHeaders,
      })
    ).status,
    401,
  )

  const login = await fetch(`${baseUrl}/api/v1/auth/demo/login`, {
    method: 'POST',
    headers: { Cookie: 'arol_q2_demo_access=stale-cookie-from-a-prior-process' },
  })
  assert.equal(login.status, 200)
  const loginBody = await login.json()
  const cookie = login.headers.get('set-cookie')

  assert.equal(loginBody.mode, 'demo')
  assert.equal(loginBody.subject, 'USR-011')
  assert.equal(loginBody.companyId, 'CMP-003')
  assert.equal(loginBody.visibility, 'full')
  assert.equal(loginBody.token, undefined)
  assert.match(loginBody.csrfToken, /^csrf-v1\./)
  assert.ok(cookie)
  assert.match(cookie, /arol_q2_demo_access=/)
  assert.match(cookie, /HttpOnly/)
  assert.doesNotMatch(cookie, /Secure/)
  assert.equal(login.headers.get('strict-transport-security'), 'max-age=31536000; includeSubDomains')

  const headers = { Cookie: cookie.split(';')[0], 'X-CSRF-Token': loginBody.csrfToken }
  const authSession = await getJson(`${baseUrl}/api/v1/auth/session`, { headers })
  assert.equal(authSession.mode, 'demo')
  assert.equal(authSession.subject, 'USR-011')
  assert.deepEqual(authSession.machineIds, [machineId])
  assert.equal(authSession.token, undefined)
  assert.equal(authSession.csrfToken, loginBody.csrfToken)

  const context = await getJson(`${baseUrl}/api/v1/machines/${machineId}/context`, { headers })
  assert.equal(context.machine.id, machineId)
  assert.equal((await fetch(`${baseUrl}/api/v1/machines/MCH-0006/context`, { headers })).status, 403)

  const logoutWithoutCsrf = await fetch(`${baseUrl}/api/v1/auth/demo/logout`, {
    method: 'POST',
    headers: { Cookie: cookie.split(';')[0] },
  })
  assert.equal(logoutWithoutCsrf.status, 403)

  const logout = await fetch(`${baseUrl}/api/v1/auth/demo/logout`, {
    method: 'POST',
    headers,
  })
  assert.equal(logout.status, 200)
  assert.match(logout.headers.get('set-cookie') ?? '', /Max-Age=0/)
})

test('gateway completes OIDC code flow into an opaque rotating browser session', async (t) => {
  const operationsDir = await mkdtemp(path.join(os.tmpdir(), 'arol-q2-oidc-ops-'))
  let issuer = ''
  const clientId = 'arol-q2-web'
  const clientSecret = 'oidc-client-secret'
  const machineId = 'MCH-0004'
  const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 })
  const publicJwk = {
    ...publicKey.export({ format: 'jwk' }),
    kid: 'oidc-provider-key',
    alg: 'RS256',
    use: 'sig',
  }
  const tokenRequests = []
  let provider

  provider = http.createServer(async (req, res) => {
    if (req.url === '/.well-known/openid-configuration') {
      sendTestJson(res, {
        issuer,
        authorization_endpoint: `http://127.0.0.1:${provider.address().port}/authorize`,
        token_endpoint: `http://127.0.0.1:${provider.address().port}/oauth2/token`,
        jwks_uri: `http://127.0.0.1:${provider.address().port}/oauth2/jwks`,
        end_session_endpoint: `http://127.0.0.1:${provider.address().port}/logout`,
      })
      return
    }

    if (req.url === '/oauth2/jwks') {
      sendTestJson(res, { keys: [publicJwk] })
      return
    }

    if (req.url === '/oauth2/token') {
      const body = await readRequestBody(req)
      const form = new URLSearchParams(body)
      tokenRequests.push(Object.fromEntries(form.entries()))

      if (form.get('client_id') !== clientId || form.get('client_secret') !== clientSecret) {
        sendTestJson(res, { error: 'invalid_client' }, 401)
        return
      }

      const isRefresh = form.get('grant_type') === 'refresh_token'
      if (
        !isRefresh
        && createHash('sha256').update(form.get('code_verifier') ?? '').digest('base64url') !== provider.codeChallenge
      ) {
        sendTestJson(res, { error: 'invalid_grant' }, 400)
        return
      }

      const subject = 'oidc-operator'
      sendTestJson(res, {
        access_token: isRefresh ? 'access-token-rotated' : 'access-token-1',
        refresh_token: isRefresh ? 'refresh-token-rotated' : 'refresh-token-1',
        expires_in: 1,
        id_token: signJwt(
          {
            iss: issuer,
            aud: clientId,
            sub: subject,
            machine_ids: [machineId],
            roles: ['operator'],
            company_id: 'CMP-003',
            visibility: 'full',
            nonce: isRefresh ? undefined : provider.nonce,
            iat: Math.floor(Date.now() / 1000),
            exp: Math.floor(Date.now() / 1000) + 300,
          },
          privateKey,
          publicJwk.kid,
        ),
      })
      return
    }

    sendTestJson(res, { error: 'Not found.' }, 404)
  })

  await listen(provider, 0)
  t.after(() => provider.close())

  const providerPort = provider.address().port
  issuer = `http://127.0.0.1:${providerPort}`
  const providerIssuer = issuer
  provider.codeChallenge = null
  provider.nonce = null

  const gateway = await startGateway({
    OPERATIONS_DATA_DIR: operationsDir,
    GATEWAY_AUTH_MODE: 'oidc',
    GATEWAY_AUTH_COOKIE_NAME: 'arol_q2_session',
    GATEWAY_AUTH_COOKIE_SECURE: 'false',
    GATEWAY_CSRF_SECRET: 'oidc-test-csrf-secret',
    OIDC_ISSUER: providerIssuer,
    OIDC_AUDIENCE: 'arol-q2-api',
    OIDC_CLIENT_ID: clientId,
    OIDC_CLIENT_SECRET: clientSecret,
    OIDC_REDIRECT_URI: 'http://127.0.0.1/callback',
    OIDC_POST_LOGOUT_REDIRECT_URI: 'http://127.0.0.1/oidc-post-logout',
    OIDC_JWKS_URI: `http://127.0.0.1:${providerPort}/oauth2/jwks`,
    OIDC_STATE_COOKIE_NAME: 'arol_q2_oidc_state',
    OIDC_LOGOUT_STATE_COOKIE_NAME: 'arol_q2_oidc_logout',
    RATE_LIMIT_MAX: '1000',
  })
  t.after(() => gateway.stop())
  t.after(() => rm(operationsDir, { recursive: true, force: true }))

  const baseUrl = `http://127.0.0.1:${gateway.port}`
  const login = await fetch(`${baseUrl}/api/v1/auth/login?returnTo=%2Fm%2F${machineId}%3Fpage%3D2`, {
    redirect: 'manual',
  })
  assert.equal(login.status, 302)
  const authorizationUrl = new URL(login.headers.get('location'))
  provider.codeChallenge = authorizationUrl.searchParams.get('code_challenge')
  provider.nonce = authorizationUrl.searchParams.get('nonce')
  assert.equal(authorizationUrl.searchParams.get('response_type'), 'code')
  assert.equal(authorizationUrl.searchParams.get('code_challenge_method'), 'S256')
  assert.ok(provider.nonce)

  const stateCookie = cookiePair(login, 'arol_q2_oidc_state')
  assert.ok(stateCookie)
  const callback = await fetch(
    `${baseUrl}/api/v1/auth/callback?code=authorization-code&state=${encodeURIComponent(
      authorizationUrl.searchParams.get('state'),
    )}`,
    {
      redirect: 'manual',
      headers: { Cookie: stateCookie },
    },
  )
  assert.equal(callback.status, 302)
  assert.equal(callback.headers.get('location'), `/m/${machineId}?page=2`)
  const sessionCookie = cookiePair(callback, 'arol_q2_session')
  assert.ok(sessionCookie)
  assert.doesNotMatch(sessionCookie, /\./)

  const authSession = await getJson(`${baseUrl}/api/v1/auth/session`, { headers: { Cookie: sessionCookie } })
  assert.equal(authSession.authenticated, true)
  assert.equal(authSession.subject, 'oidc-operator')
  assert.equal(authSession.token, undefined)
  assert.match(authSession.csrfToken, /^csrf-v1\./)
  assert.equal(tokenRequests.length, 2)
  assert.equal(tokenRequests[0].grant_type, 'authorization_code')
  assert.equal(tokenRequests[1].grant_type, 'refresh_token')
  assert.equal(tokenRequests[1].refresh_token, 'refresh-token-1')

  const csrfFailure = await fetch(`${baseUrl}/api/v1/auth/logout`, {
    method: 'POST',
    headers: { Cookie: sessionCookie },
  })
  assert.equal(csrfFailure.status, 403)

  const logout = await fetch(`${baseUrl}/api/v1/auth/logout`, {
    method: 'POST',
    headers: {
      Cookie: sessionCookie,
      'X-CSRF-Token': authSession.csrfToken,
    },
    body: JSON.stringify({ returnTo: `/m/${machineId}?page=2` }),
  })
  assert.equal(logout.status, 200)
  const logoutBody = await logout.json()
  assert.equal(logoutBody.logoutRequired, true)
  assert.match(logout.headers.get('set-cookie') ?? '', /Max-Age=0/)

  const logoutStateCookie = cookiePair(logout, 'arol_q2_oidc_logout')
  assert.ok(logoutStateCookie)
  const logoutRedirect = await fetch(`${baseUrl}/api/v1/auth/logout/redirect`, {
    redirect: 'manual',
    headers: { Cookie: logoutStateCookie },
  })
  assert.equal(logoutRedirect.status, 302)
  const providerLogoutUrl = new URL(logoutRedirect.headers.get('location'))
  assert.equal(providerLogoutUrl.pathname, '/logout')
  assert.ok(providerLogoutUrl.searchParams.get('id_token_hint'))
  assert.equal(providerLogoutUrl.searchParams.get('client_id'), clientId)
  assert.equal(
    providerLogoutUrl.searchParams.get('post_logout_redirect_uri'),
    'http://127.0.0.1/oidc-post-logout',
  )

  const postLogout = await fetch(
    `${baseUrl}/api/v1/auth/post-logout-callback?state=${encodeURIComponent(
      providerLogoutUrl.searchParams.get('state'),
    )}`,
    { redirect: 'manual' },
  )
  assert.equal(postLogout.status, 302)
  assert.equal(postLogout.headers.get('location'), `/m/${machineId}?page=2`)

  const loggedOut = await getJson(`${baseUrl}/api/v1/auth/session`)
  assert.equal(loggedOut.authenticated, false)
  assert.equal(loggedOut.subject, 'anonymous')
})

function chatResponse() {
  return {
    message: {
      id: 'assistant-message',
      role: 'assistant',
      content: 'AI response',
      createdAt: '2026-05-15T00:00:00.000Z',
    },
    agentTrace: ['supervisor'],
    intents: ['documentation'],
    routingReason: 'test',
    evidence: [],
    toolCalls: [],
    diagnosticSteps: [],
    recommendedActions: [],
    answerConfidence: 0.9,
    reviewRequired: false,
    reviewReasons: [],
  }
}

function chatSession(machineId, ownerSubject) {
  return {
    sessionId: 'ai-owned-session',
    machineId,
    ownerSubject: ownerSubject || 'anonymous',
    createdAt: '2026-05-15T00:00:00.000Z',
    expiresAt: '2026-05-15T08:00:00.000Z',
    messageCount: 0,
    messages: [],
  }
}

function reviewResponse() {
  return {
    ...chatResponse(),
    message: {
      id: 'assistant-review-message',
      role: 'assistant',
      content: 'I cannot help bypass safety protections. Stop and escalate to a qualified technician.',
      createdAt: '2026-05-15T00:00:00.000Z',
    },
    intents: ['safety'],
    routingReason: 'safety request detected',
    recommendedActions: [
      {
        label: 'Stop the machine and restore safety protections.',
        priority: 'immediate',
        requiresTechnician: false,
        source: 'safety',
      },
    ],
    answerConfidence: 0.2,
    reviewRequired: true,
    reviewReasons: ['safety', 'low-confidence', 'immediate-action'],
  }
}

function manualIndexStatus() {
  return {
    ragEnabled: true,
    collection: 'manual_chunks',
    embeddingModel: 'nomic-embed-text',
    status: 'indexed',
    totalIndexedChunks: 3,
    manuals: [
      {
        machineId: 'MCH-0004',
        title: 'AROL Euro VP - Use and Maintenance Manual',
        version: 'Z17478ABSEN001-0',
        language: 'en',
        fileName: 'Original manual 2019 Arol Euro VIP.pdf',
        sourceUri: '/manuals/Original%20manual%202019%20Arol%20Euro%20VIP.pdf',
        pdfExists: true,
        indexedChunkCount: 3,
        status: 'indexed',
      },
    ],
  }
}

function manualSearchResponse() {
  return {
    machineId: 'MCH-0004',
    query: 'torque alarm',
    count: 1,
    evidence: [
      {
        source: 'manual',
        title: 'Cap Torque Alarm Troubleshooting',
        excerpt: 'Inspect the capper head and verify chuck wear.',
        page: 2,
        confidence: 0.9,
        manualVersion: 'Z17478ABSEN001-0',
        language: 'en',
        sourceUri: '/manuals/Original%20manual%202019%20Arol%20Euro%20VIP.pdf',
        chunkId: 'chunk-1',
        section: 'Cap Torque Alarm Troubleshooting',
        score: 0.9,
      },
    ],
  }
}

function telemetrySample(machineId) {
  return {
    machineId,
    timestamp: '2030-01-02T03:04:05.678Z',
    rpm: 96.4,
    torqueNm: 8.42,
    temperatureC: 41.1,
    activeAlarm: 'TORQUE_HIGH',
    health: 'warning',
    source: 'iot-simulator',
    quality: 'fresh',
    ageSeconds: 0,
    staleAfterSeconds: 900,
    missingFields: [],
  }
}

// The dataset carries no warranty or SLA, so neither does this fixture: a
// stub richer than the real source would let a regression pass here and fail
// against the data.
function serviceContract(machineId) {
  return {
    machineId,
    companyId: 'CMP-001',
    serialNumber: '15610',
    modelCode: 'TS-EURO-PK-TWIN-CHUTE-D',
    deliveryDate: '2021-05-14',
    plantLocation: 'Novara Plant 1 - Bottling Line 3',
    acquisitionCost: 128400,
    currency: 'EUR',
    openTicketCount: 2,
    ticketCount: 9,
    lastScheduledMaintenance: '2026-07-11',
    coverageNote:
      'The fleet dataset contains no warranty or SLA records. Coverage questions must be referred to AROL service.',
  }
}

function sendTestJson(res, payload, statusCode = 200) {
  res.writeHead(statusCode, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(payload))
}

async function readRequestBody(req) {
  const chunks = []
  for await (const chunk of req) {
    chunks.push(chunk)
  }
  return Buffer.concat(chunks).toString('utf8')
}

function cookiePair(response, name) {
  const values = typeof response.headers.getSetCookie === 'function'
    ? response.headers.getSetCookie()
    : (response.headers.get('set-cookie') ?? '').split(/,(?=\s*[^;=]+=[^;]+)/)
  const cookie = values.find(
    (value) => value.trim().startsWith(`${name}=`) && !/Max-Age=0/.test(value),
  )
  return cookie?.split(';')[0]
}

function parseJson(value) {
  try {
    return value ? JSON.parse(value) : null
  } catch {
    return null
  }
}

function signJwt(claims, privateKey, kid) {
  const header = {
    alg: 'RS256',
    typ: 'JWT',
    kid,
  }
  const encodedHeader = encodeJwtPart(header)
  const encodedPayload = encodeJwtPart(claims)
  const signingInput = `${encodedHeader}.${encodedPayload}`
  const signature = sign('RSA-SHA256', Buffer.from(signingInput), privateKey)

  return `${signingInput}.${signature.toString('base64url')}`
}

function encodeJwtPart(value) {
  return Buffer.from(JSON.stringify(value)).toString('base64url')
}

async function startGateway(env) {
  const port = await openPort()
  const child = spawn(process.execPath, ['src/server.js'], {
    cwd: gatewayRoot,
    env: {
      ...process.env,
      PORT: String(port),
      MANUALS_DIR: path.join(repoRoot, 'requirements', 'manuals'),
      // Authentication is on by default now. Tests that exercise auth set the
      // mode themselves; the rest run without it, as they did before.
      GATEWAY_AUTH_MODE: 'off',
      ...env,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  let output = ''
  child.stdout.on('data', (chunk) => {
    output += chunk.toString()
  })
  child.stderr.on('data', (chunk) => {
    output += chunk.toString()
  })

  await waitForHealth(`http://127.0.0.1:${port}/health`, child, () => output)

  return {
    port,
    output() {
      return output
    },
    // Wait for the process to actually go. It holds the operations database
    // open, and on Windows the temporary directory cannot be removed until it
    // has let go.
    async stop() {
      if (child.exitCode !== null || child.signalCode !== null) {
        return
      }
      child.kill()
      await once(child, 'exit')
    },
  }
}

async function waitUntil(predicate, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await predicate()) {
      return
    }
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
  throw new Error('Timed out waiting for condition.')
}

async function rawHttpRequest(port, requestText) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host: '127.0.0.1', port })
    let response = ''
    socket.setEncoding('utf8')
    socket.on('connect', () => socket.write(requestText))
    socket.on('data', (chunk) => {
      response += chunk
    })
    socket.on('end', () => resolve(response))
    socket.on('error', reject)
  })
}

async function getJson(url, init) {
  const response = await fetch(url, init)
  if (response.status !== 200 && response.status !== 201) {
    assert.fail(await response.text())
  }
  return response.json()
}

async function postJson(url, body, init) {
  const response = await fetch(url, {
    ...init,
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
    body: JSON.stringify(body),
  })
  if (response.status !== 200 && response.status !== 201) {
    assert.fail(await response.text())
  }
  return response.json()
}

async function waitForHealth(url, child, readOutput = () => '') {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (child.exitCode !== null) {
      // Include what the gateway said. Without this the failure is just an
      // exit code, which is nearly useless when it only reproduces in CI.
      throw new Error(
        `Gateway exited with code ${child.exitCode}.\n${readOutput().trim() || '(no output)'}`,
      )
    }

    try {
      const response = await fetch(url)
      if (response.ok) {
        return
      }
    } catch {
      await sleep(100)
    }
  }

  throw new Error('Gateway did not become healthy in time.')
}

async function openPort() {
  const server = net.createServer()
  await listen(server, 0)
  const { port } = server.address()
  await new Promise((resolve) => server.close(resolve))
  return port
}

async function listen(server, port) {
  server.listen(port, '127.0.0.1')
  await once(server, 'listening')
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms)
  })
}
