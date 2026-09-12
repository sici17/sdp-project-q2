import { expect, test, type Page, type Route } from '@playwright/test'

test('mobile explains a refused machine instead of implying no alarms', async ({ page }) => {
  await mockGateway(page)
  await page.route('**/api/v1/machines/*/context', route => json(route,
    { code: 'machine_not_in_company', error: 'Forbidden' }, 403))
  await page.goto(`/m/${machineId}`)
  await expect(page.getByRole('alert')).toContainText('another company')
  await expect(page.getByRole('link', { name: 'Back to fleet' })).toBeVisible()
  await expect(page.getByText(/No active alarm/)).toHaveCount(0)
  await expect(page.getByText('Loading machine')).toHaveCount(0)
})

test('manual search returns pages and zoom provides selectable document text', async ({ page }) => {
  await mockGateway(page)
  await page.route('**/api/v1/manuals/search', route => json(route, {
    machineId, query: 'pressure', count: 1,
    evidence: [{ source: 'manual', title: 'Pressure check', excerpt: 'Check the supply pressure.', page: 1 }],
  }))
  await page.goto(`/m/${machineId}`)
  await page.getByRole('button', { name: 'Open the manual', exact: true }).click()
  const manual = page.getByRole('region', { name: 'Manual', exact: true })
  const canvas = manual.getByRole('img')
  await expect(canvas).toBeVisible()
  await expect(manual.locator('.textLayer')).toContainText('Mobile manual')
  const initialWidth = (await canvas.boundingBox())!.width
  await manual.getByRole('button', { name: 'Zoom in', exact: true }).click()
  await expect.poll(async () => (await canvas.boundingBox())!.width).toBeGreaterThan(initialWidth)
  await manual.getByRole('button', { name: 'Search the manual', exact: true }).click()
  await page.getByRole('textbox', { name: 'Search this manual' }).fill('pressure')
  await page.getByRole('button', { name: 'Search', exact: true }).click()
  const result = page.getByRole('button', { name: /Page 1 — Pressure check/ })
  await expect(result).toHaveCSS('border-radius', '8px')
  await result.click()
  await expect(manual.getByText(/MANUAL.*CITED/)).toHaveCount(0)
  await expect(page.locator('#machine-context-panel.open')).toHaveCount(0)
  await expect(manual.getByText('1 / 1', { exact: true })).toBeVisible()
  await expect(manual.getByRole('textbox', { name: 'Page number' })).toHaveCount(0)
  await expect(manual.getByRole('link', { name: 'Original PDF' })).toBeVisible()
  await expect(canvas).toHaveAttribute('aria-label', /page 1 of 1/)
})

test('commercial drawer never renders operational ticket counts from a legacy payload', async ({ page }) => {
  await mockGateway(page, { auth: { ...authSession, authenticated: true,
    companyId: 'CMP-001', visibility: 'commercial', domains: ['common', 'commercial'] } })
  await page.goto(`/m/${machineId}`)
  await page.getByRole('button', { name: 'Open machine context' }).click()
  const drawer = page.locator('#machine-context-panel.open')
  await expect(drawer.getByText(/Maintenance ·/)).toHaveCount(0)
  await expect(drawer.getByText('Open tickets', { exact: true })).toHaveCount(0)
  await expect(drawer.getByText(/does not include telemetry, alarms and maintenance history/)).toBeVisible()
})

const machineId = 'euro-vp-2019-01'
const manualPath = '/manuals/EURO-VP-capping-head.pdf'

function buildPdfFixture() {
  const stream = 'BT\n/F1 24 Tf\n72 720 Td\n(Mobile manual) Tj\nET\n'
  const objects = [
    '1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n',
    '2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n',
    '3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>\nendobj\n',
    `4 0 obj\n<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}endstream\nendobj\n`,
    '5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n',
  ]
  let pdf = '%PDF-1.4\n'
  const offsets = [0]
  for (const object of objects) {
    offsets.push(Buffer.byteLength(pdf))
    pdf += object
  }
  const xrefOffset = Buffer.byteLength(pdf)
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
  pdf += offsets
    .slice(1)
    .map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`)
    .join('')
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`
  return Buffer.from(pdf)
}

const pdfFixture = buildPdfFixture()

const authSession = {
  mode: 'off',
  subject: 'mobile-e2e-operator',
  roles: ['operator'],
  machineIds: [],
  hasMachineRestriction: false,
  authenticated: false,
  csrfToken: 'csrf-e2e-token',
}

const context = {
  machine: {
    id: machineId,
    serialNumber: 'E2E-001',
    model: 'EURO VP',
    plant: 'Canelli',
    status: 'healthy',
    lastTelemetryAt: '2026-07-11T08:00:00.000Z',
  },
  manual: {
    machineId,
    title: 'EURO VP Operating Manual',
    version: '1.0',
    language: 'en',
    url: manualPath,
  },
  documents: [],
  // The shape the fleet dataset publishes. There is no rpm or torque in it, and
  // a fixture richer than the real source lets the console regress into showing
  // fields that will never arrive.
  telemetry: {
    machineId,
    timestamp: '2026-07-11T08:00:00.000Z',
    operationalStatus: 'Running',
    productionRateBph: 3600,
    nominalRateBph: 4500,
    rateUtilizationPct: 80,
    uptimePercentage: 92.5,
    alarmCount: 0,
    temperatureC: 42,
    energyKwh: 18.4,
    activeAlarm: null,
    health: 'healthy',
    source: 'fleet-dataset',
    quality: 'fresh',
    ageSeconds: 5,
    staleAfterSeconds: 900,
    missingFields: [],
  },
  contract: {
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
    coverageNote: 'The fleet dataset contains no warranty or SLA records.',
  },
}

const manualIndex = {
  ragEnabled: true,
  collection: 'manual_chunks',
  embeddingModel: 'nomic-embed-text',
  status: 'indexed',
  totalIndexedChunks: 12,
  manuals: [
    {
      machineId,
      title: 'EURO VP Operating Manual',
      version: '1.0',
      language: 'en',
      fileName: 'EURO-VP-capping-head.pdf',
      sourceUri: manualPath,
      pdfExists: true,
      indexedChunkCount: 12,
      status: 'indexed',
    },
  ],
}

const longAnswer = [
  '### Recommended checks',
  '**Safety:** Keep the guard active while following the cited procedure.',
  ...Array.from(
    { length: 42 },
    (_, index) => `${index + 1}. Verify the guarded torque setting before restarting the machine.`,
  ),
].join('\n')

const diagnosticSteps = Array.from({ length: 8 }, (_, index) => ({
  label: `Diagnostic check ${index + 1}`,
  detail:
    'Keep guards and interlocks enabled while recording the alarm and checking the cited procedure.',
  priority: 'high' as const,
  requiresTechnician: index > 3,
  source: 'manual',
  evidenceTitle: 'Torque procedure',
  page: 81,
  expectedOutcome: 'The check is recorded without bypassing a safety device.',
  safetyLevel: index > 3 ? ('technician' as const) : ('operator' as const),
  requiredRole: index > 3 ? 'technician' : 'operator',
  stepId: `diagnostic-${index + 1}`,
}))

const chatResponse = {
  message: {
    id: 'assistant-mobile-1',
    role: 'assistant',
    content: longAnswer,
    createdAt: '2026-07-11T08:01:00.000Z',
  },
  agentTrace: ['supervisor', 'doc-agent'],
  intents: ['manual-search'],
  routingReason: 'The request requires a manual procedure.',
  evidence: [
    {
      source: 'manual',
      title: 'Torque procedure',
      section: 'Torque check',
      excerpt: 'Verify the torque setting before restarting.',
      page: 81,
      manualVersion: '1.0',
      sourceUri: manualPath,
      chunkId: 'mobile-e2e-chunk-81',
      confidence: 0.96,
      chunkKind: 'procedure',
      topics: ['torque'],
      alarmCodes: [],
      safetyLevel: 'operator',
    },
  ],
  toolCalls: [],
  diagnosticSteps,
  recommendedActions: [],
  answerConfidence: 0.96,
  reviewRequired: false,
  reviewReasons: [],
}

async function json(route: Route, payload: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(payload),
  })
}

const demoUsers = [
  {
    userId: 'USR-0007',
    name: 'Nuria Castellon',
    email: 'nuria.castellon@example.com',
    jobTitle: 'Maintenance Lead',
    visibility: 'technician',
    companyId: 'CMP-001',
    companyName: 'Farmaceutica del Ebro',
    country: 'ES',
    domains: ['common', 'operational'],
  },
]

interface MockGatewayOptions {
  auth?: typeof authSession & {
    userId?: string
    companyId?: string
    visibility?: string
    domains?: string[]
  }
  /** A machine context that differs from the default fixture. */
  context?: typeof context
  resumedSession?: {
    sessionId: string
    machineId: string
    messages: Array<{
      id: string
      role: 'user' | 'assistant'
      content: string
      createdAt: string
    }>
  }
  failStreamAttempts?: number
  failSessionAttempts?: number
  contextDelayMs?: number
  streamDelayMs?: number
}

interface ChatRequestObservation {
  url: string
  idempotencyKey: string | null
  body: Record<string, unknown>
}

async function mockGateway(page: Page, options: MockGatewayOptions = {}) {
  const pdfRequests: string[] = []
  const streamRequests: ChatRequestObservation[] = []
  const sessionRequests: ChatRequestObservation[] = []
  let sessionCreates = 0
  let messageRequests = 0
  let remainingStreamFailures = options.failStreamAttempts ?? 0
  let remainingSessionFailures = options.failSessionAttempts ?? 0

  await page.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname

    if (path.startsWith('/manuals/')) {
      pdfRequests.push(request.url())
      await route.fulfill({
        contentType: 'application/pdf',
        headers: { 'Content-Disposition': 'inline' },
        body: pdfFixture,
      })
      return
    }

    if (path === '/api/v1/auth/demo/users') {
      await json(route, { users: demoUsers })
      return
    }

    if (path === '/api/v1/auth/demo/login' || path === '/api/v1/auth/session') {
      await json(route, options.auth ?? authSession)
      return
    }

    if (path === `/api/v1/machines/${machineId}/context`) {
      if (options.contextDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.contextDelayMs))
      }
      await json(route, options.context ?? context)
      return
    }

    if (path === '/api/v1/chat/sessions' && request.method() === 'POST') {
      sessionCreates += 1
      sessionRequests.push({
        url: request.url(),
        idempotencyKey: request.headers()['idempotency-key'] ?? null,
        body: request.postDataJSON() as Record<string, unknown>,
      })
      if (remainingSessionFailures > 0) {
        remainingSessionFailures -= 1
        await route.abort('connectionreset')
        return
      }
      await json(route, {
        sessionId: 'mobile-e2e-session',
        machineId,
        messages: [],
      })
      return
    }

    if (path.startsWith('/api/v1/chat/sessions/') && request.method() === 'GET') {
      if (options.resumedSession && path.endsWith(`/${options.resumedSession.sessionId}`)) {
        await json(route, options.resumedSession)
      } else {
        await json(route, { error: 'session not found' }, 404)
      }
      return
    }

    if (path === '/api/v1/manuals/index/status') {
      await json(route, manualIndex)
      return
    }

    if (path === '/api/v1/chat/stream') {
      streamRequests.push({
        url: request.url(),
        idempotencyKey: request.headers()['idempotency-key'] ?? null,
        body: request.postDataJSON() as Record<string, unknown>,
      })

      if (remainingStreamFailures > 0) {
        remainingStreamFailures -= 1
        await json(route, { error: 'temporary stream interruption' }, 502)
        return
      }

      if (options.streamDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.streamDelayMs))
      }

      const trace = JSON.stringify({
        agentTrace: ['supervisor', 'doc-agent'],
        intents: ['manual-search'],
        routingReason: 'The request requires a manual procedure.',
      })
      const token = JSON.stringify({ delta: 'Grounded manual answer ready. ' })
      const done = JSON.stringify(chatResponse)
      await route.fulfill({
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache' },
        body: `event: trace\ndata: ${trace}\n\nevent: token\ndata: ${token}\n\nevent: done\ndata: ${done}\n\n`,
      })
      return
    }

    if (path === '/api/v1/chat/messages') {
      messageRequests += 1
      await json(route, chatResponse)
      return
    }

    if (path.startsWith('/api/v1/')) {
      await json(route, {})
      return
    }

    await route.continue()
  })

  return {
    pdfRequests,
    streamRequests,
    sessionRequests,
    get sessionCreates() {
      return sessionCreates
    },
    get messageRequests() {
      return messageRequests
    },
  }
}

test('opens the manual as a sheet over the chat, and opens/closes the mobile context drawer', async ({
  page,
}) => {
  const observations = await mockGateway(page)

  await page.goto(`/m/${machineId}`)

  // The phone opens on the conversation; the manual is one tap away and comes
  // back over it rather than replacing it.
  await expect(page.getByRole('region', { name: 'Machine support' })).toBeVisible()
  await page.getByRole('button', { name: 'Open the manual' }).click()

  const manualPanel = page.getByRole('region', { name: 'Manual' })
  await expect(manualPanel).toBeVisible()
  await expect(manualPanel.getByRole('heading', { name: 'EURO VP Operating Manual' })).toBeVisible()
  await expect.poll(() => observations.pdfRequests.length).toBeGreaterThan(0)
  const renderedPage = manualPanel.getByRole('img', { name: /EURO VP Operating Manual, page 1 of 1/ })
  await expect(renderedPage).toBeVisible()
  await expect.poll(async () => Number(await renderedPage.getAttribute('width'))).toBeGreaterThan(0)

  await manualPanel.getByRole('button', { name: 'Close' }).click()

  // A CSS locator, because the open drawer aria-hides the page behind it and
  // role queries then - correctly - stop resolving the trigger.
  const menu = page.locator('[aria-label="Open machine context"]')
  await expect(menu).toHaveAttribute('aria-expanded', 'false')
  await menu.click()
  await expect(menu).toHaveAttribute('aria-expanded', 'true')
  const drawer = page.locator('#machine-context-panel.open')
  await expect(drawer).toBeVisible()
  await expect(drawer).toHaveAttribute('role', 'dialog')
  await expect(page.locator('body')).toHaveCSS('overflow', 'hidden')
  await expect(drawer.getByRole('button', { name: 'Close' })).toBeFocused()

  await page.keyboard.press('Escape')
  await expect(menu).toHaveAttribute('aria-expanded', 'false')
  await expect(page.locator('#machine-context-panel.open')).toHaveCount(0)
  await expect(menu).toBeFocused()
})

test('keeps chat scrolling internal and citation navigation jumps to the cited page', async ({ page }) => {
  await mockGateway(page)

  await page.goto(`/m/${machineId}`)
  const chatPanel = page.getByRole('region', { name: 'Machine support' })
  const messageInput = chatPanel.getByRole('textbox', { name: 'Message' })
  await expect(messageInput).toBeEnabled()
  await messageInput.fill('How do I verify the torque setting?')
  await chatPanel.getByRole('button', { name: 'Send' }).click()

  await expect(
    chatPanel.getByText('Verify the guarded torque setting before restarting the machine.', {
      exact: true,
    }).first(),
  ).toBeVisible()
  const chatList = chatPanel.locator('.chat-list')
  await expect.poll(async () => chatList.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)
  await expect(chatList).toHaveCSS('overflow-y', 'auto')

  await chatPanel.getByRole('button', { name: 'View 1 source' }).click()
  const evidenceDialog = page.getByRole('dialog', { name: /1 source/ })
  await expect(evidenceDialog).toBeVisible()
  await expect(evidenceDialog.getByText('Torque check')).toBeVisible()

  await evidenceDialog.getByRole('button', { name: 'Show page 81' }).click()
  await expect(evidenceDialog).toHaveCount(0)
  expect(new URL(page.url()).searchParams.get('page')).toBe('81')
  await expect(page.getByRole('img', { name: /EURO VP Operating Manual, page 1 of 1/ })).toBeVisible()
})

test('resumes the machine session and keeps historic citations accessible', async ({ page }) => {
  const resumedSession = {
    sessionId: 'persisted-mobile-session',
    machineId,
    messages: [
      {
        id: 'historic-user',
        role: 'user' as const,
        content: 'What was the torque procedure?',
        createdAt: '2026-07-11T07:59:00.000Z',
      },
      {
        id: 'historic-assistant',
        role: 'assistant' as const,
        content: 'Use the cited guarded torque procedure.',
        createdAt: '2026-07-11T08:00:00.000Z',
      },
    ],
  }
  await page.addInitScript(
    ({ id, machine, historicEvidence }) => {
      window.sessionStorage.setItem(`arol-q2:chat-session:${machine}`, id)
      window.sessionStorage.setItem(
        `arol-q2:chat-evidence:${id}`,
        JSON.stringify({ 'historic-assistant': historicEvidence }),
      )
    },
    {
      id: resumedSession.sessionId,
      machine: machineId,
      historicEvidence: [
        ...chatResponse.evidence,
        {
          source: 'manual',
          title: 'Manual',
          section: 'Manual',
          excerpt:
            'AROL S.p.A. - teaching copy, Politecnico di Torino, System and Device Programming. Do not redistribute.',
          page: 3,
        },
        {
          source: 'manual',
          title: 'Report of the training',
          section: 'Report of the training',
          excerpt: 'Trainer/s signature/s: Mr. ______\nNAME | CHARGE | SIGNATURE',
          page: 82,
        },
      ],
    },
  )
  const observations = await mockGateway(page, { resumedSession })

  await page.goto(`/m/${machineId}`)

  await expect(page.getByText('Use the cited guarded torque procedure.')).toBeVisible()
  await expect.poll(() => observations.sessionCreates).toBe(0)
  await page.getByRole('button', { name: 'View 1 source' }).click()
  const dialog = page.getByRole('dialog', { name: /1 source/ })
  await expect(dialog.getByText('Torque check')).toBeVisible()
  await expect(dialog.getByText('Report of the training')).toHaveCount(0)
  await expect(dialog.getByText(/teaching copy/i)).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: 'Close' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'View 1 source' })).toBeFocused()
})
