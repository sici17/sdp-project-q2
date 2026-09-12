import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * The journey a customer actually takes: choose who you are, see your own
 * machines, open one.
 *
 * These tests also pin the two things the access model has to show on screen —
 * a locked panel rather than a hidden one, and an honest empty fleet rather
 * than a refusal — because both are easy to regress into looking like missing
 * data.
 */

const MACHINE_ID = 'MCH-0004'

const USERS = [
  {
    userId: 'USR-011',
    name: 'Nuria Castellon',
    email: 'nuria.castellon@ebro.example',
    jobTitle: 'Engineering Manager',
    visibility: 'full',
    companyId: 'CMP-003',
    companyName: 'Farmaceutica del Ebro S.L.',
    country: 'Spain',
    domains: ['common', 'operational', 'commercial'],
  },
  {
    userId: 'USR-012',
    name: 'Joaquin Peralta',
    email: 'joaquin.peralta@ebro.example',
    jobTitle: 'Maintenance Man',
    visibility: 'technician',
    companyId: 'CMP-003',
    companyName: 'Farmaceutica del Ebro S.L.',
    country: 'Spain',
    domains: ['common', 'operational'],
  },
  {
    userId: 'USR-019',
    name: 'Rui Cardoso',
    email: 'rui.cardoso@cascais.example',
    jobTitle: 'Procurement Lead',
    visibility: 'commercial',
    companyId: 'CMP-005',
    companyName: 'Aguas de Cascais Lda',
    country: 'Portugal',
    domains: ['common', 'commercial'],
  },
]

function sessionFor(userId: string) {
  const user = USERS.find((candidate) => candidate.userId === userId) ?? USERS[0]
  return {
    mode: 'demo',
    subject: user.userId,
    roles: ['operator'],
    machineIds: [],
    hasMachineRestriction: false,
    authenticated: true,
    csrfToken: 'csrf-fleet-token',
    userId: user.userId,
    companyId: user.companyId,
    visibility: user.visibility,
    domains: user.domains,
  }
}

/** The gateway's configured fallback identity: full access to CMP-003. */
const DEFAULT_USER_ID = 'USR-011'

/** Every demo login the page makes, in order. */
const demoLogins: string[] = []

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

/**
 * Mock gateway that tracks who is signed in, so visibility can change mid-test.
 *
 * Pass `null` for a browser that arrives with no session at all, which is what
 * a first visit and a QR scan both look like.
 */
async function mockGateway(page: Page, initialUserId: string | null) {
  let currentUserId = initialUserId
  demoLogins.length = 0

  await page.route('**/*', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname

    if (path === '/api/v1/auth/demo/users') {
      await json(route, { users: USERS })
      return
    }

    if (path === '/api/v1/auth/demo/login') {
      const body = request.postDataJSON?.() ?? {}
      // A login with no user is the gateway's default identity, which is a
      // different person from whoever is signed in.
      currentUserId = body.userId ?? DEFAULT_USER_ID
      demoLogins.push(currentUserId)
      await json(route, sessionFor(currentUserId))
      return
    }

    if (path === '/api/v1/auth/session') {
      if (currentUserId === null) {
        await json(route, {
          mode: 'demo',
          subject: 'anonymous',
          roles: [],
          machineIds: [],
          hasMachineRestriction: false,
          authenticated: false,
        })
        return
      }
      await json(route, sessionFor(currentUserId))
      return
    }

    if (path === '/api/v1/machines') {
      const session = sessionFor(currentUserId ?? DEFAULT_USER_ID)
      // CMP-005 has users but owns no machines: a real state in the dataset.
      const machines =
        session.companyId === 'CMP-005'
          ? []
          : [
              {
                id: MACHINE_ID,
                companyId: session.companyId,
                serialNumber: '17478',
                model: 'M-EURO-VP-IES',
                plant: 'Zaragoza Plant 1 - Filling Suite B',
                nominalRateBph: 4500,
                headsCount: 3,
                status: session.domains.includes('operational') ? 'critical' : 'unknown',
                lastTelemetryAt: '2026-08-04T23:00Z',
              },
            ]
      await json(route, { machines })
      return
    }

    if (path.endsWith('/context')) {
      const session = sessionFor(currentUserId ?? DEFAULT_USER_ID)
      await json(route, {
        machine: {
          id: MACHINE_ID,
          companyId: session.companyId,
          serialNumber: '17478',
          model: 'M-EURO-VP-IES',
          plant: 'Zaragoza Plant 1 - Filling Suite B',
          status: session.domains.includes('operational') ? 'critical' : 'unknown',
        },
        manual: {
          machineId: MACHINE_ID,
          title: 'M-EURO-VP-IES - use and maintenance manual, serial 17478',
          version: null,
          language: 'en',
          url: '/manuals/17478_manual_EN.pdf',
          serialNumber: '17478',
          relatedManuals: [],
        },
        documents: [],
        telemetry: null,
        contract: null,
        withheld: session.domains.includes('commercial')
          ? []
          : [{ domain: 'commercial', reason: 'visibility_denied' }],
      })
      return
    }

    if (path === '/api/v1/quotes') {
      await json(route, {
        quotes: [
          {
            quoteId: 'QTE-2025-0004',
            companyId: 'CMP-003',
            description: 'VP750 head and gripper wearing parts',
            currency: 'EUR',
            createdAt: '2025-06-02',
            validUntil: '2025-07-17',
            currentRevisionNumber: 2,
            currentRevisionStatus: 'Approved',
            currentTotal: 18240,
            revisionCount: 2,
            currentDiscountRate: 0.05,
            currentChangeSummary: 'Scope reduced, 5% discount applied',
            expired: 1,
          },
        ],
      })
      return
    }

    if (path === '/api/v1/chat/sessions' && request.method() === 'POST') {
      await json(route, {
        sessionId: 'fleet-e2e-session',
        machineId: MACHINE_ID,
        messages: [],
      })
      return
    }

    if (path === '/api/v1/manuals/index/status') {
      await json(route, { collection: 'manual_chunks', indexedChunks: 412, manuals: [] })
      return
    }

    if (path.endsWith('/orders')) {
      await json(route, [])
      return
    }

    if (path.endsWith('/maintenance')) {
      await json(route, { tickets: [] })
      return
    }

    if (path.startsWith('/manuals/')) {
      await route.fulfill({ contentType: 'application/pdf', body: '%PDF-1.7\n% fixture\n' })
      return
    }

    if (path.includes('/api/v1/')) {
      await json(route, {}, 404)
      return
    }

    await route.fallback()
  })
}

test('signs in as a dataset user and opens a machine from the fleet', async ({ page }) => {
  await mockGateway(page, 'USR-011')
  await page.goto('/login')

  // Identities are grouped by company and labelled with what they can reach,
  // so the access model is legible before anyone signs in.
  await expect(page.getByRole('heading', { name: 'Farmaceutica del Ebro S.L.' })).toBeVisible()
  await page.getByRole('button', { name: /Nuria Castellon/ }).click()

  await expect(page.getByRole('heading', { name: 'Machines' })).toBeVisible()
  await expect(page.getByText('Farmaceutica del Ebro S.L.')).toBeVisible()
  await page.getByRole('button', { name: /M-EURO-VP-IES/ }).click()

  // The phone workspace opens on the conversation, with the machine named in
  // the strip above it; the manual is a tap away in a sheet.
  await expect(page.getByRole('region', { name: 'Machine support' })).toBeVisible()
  await expect(page.locator('.workspace-header')).toContainText('M-EURO-VP-IES')
  await expect(page).toHaveURL(new RegExp(`/machines/${MACHINE_ID}$`))
})

test('a company that owns no machines gets an honest empty fleet, not a refusal', async ({
  page,
}) => {
  await mockGateway(page, 'USR-019')
  await page.goto('/')

  await expect(page.getByText(/No machines are registered to your company/)).toBeVisible()
  // An empty fleet is a true answer about their own data, so nothing on the
  // page should read as an access failure.
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('a domain the user cannot read is locked in place, not hidden', async ({ page }) => {
  await mockGateway(page, 'USR-012') // technician: no commercial data
  await page.goto(`/machines/${MACHINE_ID}`)

  // The machine context sits behind a drawer on a phone.
  await page.getByRole('button', { name: 'Open machine context' }).click()

  const locked = page.getByRole('note').filter({ hasText: 'Restricted' })
  await expect(locked.first()).toBeVisible()
  await expect(page.getByText(/technician role does not include quotations/)).toBeVisible()

  // Two commercial surfaces are locked for a technician - the service card and
  // the quotations panel - and the operational ones stay available.
  await expect(locked).toHaveCount(2)
  await expect(page.getByText(/delivery, acquisition value/)).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Maintenance' }).first()).toBeAttached()
})

test('an unknown path goes to the fleet rather than some other machine', async ({ page }) => {
  await mockGateway(page, 'USR-011')
  await page.goto('/somewhere-else')

  // Substituting a default machine would show the wrong manual for the machine
  // an operator is standing in front of.
  await expect(page.getByRole('heading', { name: 'Machines' })).toBeVisible()
  await expect(page.locator('iframe')).toHaveCount(0)
})


test('opening a machine does not silently re-sign-in as the default user', async ({ page }) => {
  // The machine workspace used to bootstrap a demo session on every mount. With
  // a technician signed in that call carried no user id, so the gateway handed
  // back its configured default — a full-access identity — and the operator
  // silently gained access they had not been granted.
  await mockGateway(page, 'USR-012')
  await page.goto('/login')
  await page.getByRole('button', { name: /Joaquin Peralta/ }).click()
  await expect(page.getByRole('heading', { name: 'Machines' })).toBeVisible()
  await expect(page.getByText('Farmaceutica del Ebro S.L.')).toBeVisible()

  await page.getByRole('button', { name: /M-EURO-VP-IES/ }).click()
  await expect(page.getByRole('region', { name: 'Machine support' })).toBeVisible()

  expect(demoLogins).toEqual(['USR-012'])
  expect(demoLogins).not.toContain(DEFAULT_USER_ID)
})

test('a first visit is asked who it is, not signed in for', async ({ page }) => {
  // The console used to POST /auth/demo/login on load. That call names no user,
  // so the gateway answered with its configured default - USR-011, full access
  // to CMP-003 - and every visitor silently became that person. The access
  // model was then the one thing the demo never actually showed.
  await mockGateway(page, null)
  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Farmaceutica del Ebro S.L.' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Machines' })).toHaveCount(0)
  expect(demoLogins).toEqual([])
})

test('a scanned machine survives the sign-in it now requires', async ({ page }) => {
  // The bootstrap existed so a QR scan needed no sign-in step. Removing it must
  // not cost the scan: an operator standing at a machine should reach that
  // machine, not a fleet list they then have to search.
  await mockGateway(page, null)
  await page.goto(`/machines/${MACHINE_ID}`)

  await expect(page.getByRole('heading', { name: 'Farmaceutica del Ebro S.L.' })).toBeVisible()
  await page.getByRole('button', { name: /Joaquin Peralta/ }).click()

  await expect(page).toHaveURL(new RegExp(`/machines/${MACHINE_ID}$`))
  await expect(page.getByRole('region', { name: 'Machine support' })).toBeVisible()
  expect(demoLogins).toEqual(['USR-012'])
})
