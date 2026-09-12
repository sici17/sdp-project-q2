/**
 * The dataset's access model, end to end through the gateway.
 *
 * Two independent checks must both pass before any data is returned: the
 * company tenant boundary, which is never crossed, and the user's visibility,
 * which narrows which data domains they reach inside their own company.
 *
 * A request outside a user's scope must be declined explicitly. These tests
 * assert the declines are real HTTP failures with a machine-readable reason,
 * not 200 responses carrying an empty list, because an operator cannot tell an
 * empty list apart from a machine with nothing to report.
 */

import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import net from 'node:net'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const gatewayRoot = path.resolve(__dirname, '..')
const repoRoot = path.resolve(gatewayRoot, '..')

// Fleet ownership in the supplied dataset.
const CMP_001_MACHINE = 'MCH-0001' // Valgrande
const CMP_004_MACHINE = 'MCH-0006' // Kilbrannan

const USERS = {
  full: 'USR-001', // Elena Fabbri, CMP-001
  technician: 'USR-002', // Matteo Bonetti, CMP-001
  commercial: 'USR-004', // Davide Ranieri, CMP-001
  otherCompany: 'USR-019', // Rui Cardoso, CMP-005, which owns no machines
}

const OPERATIONAL_PATHS = [
  (id) => `/api/v1/machines/${id}/telemetry/latest`,
  (id) => `/api/v1/machines/${id}/telemetry/history?limit=2`,
  (id) => `/api/v1/machines/${id}/alarms`,
  (id) => `/api/v1/machines/${id}/service-history`,
]

const COMMERCIAL_PATHS = [
  (id) => `/api/v1/machines/${id}/orders`,
  (id) => `/api/v1/machines/${id}/contract`,
  (id) => `/api/v1/machines/${id}/warranty`,
]

const COMMON_PATHS = [
  (id) => `/api/v1/machines/${id}`,
  (id) => `/api/v1/machines/${id}/context`,
  (id) => `/api/v1/machines/${id}/documents`,
  (id) => `/api/v1/manuals/${id}`,
]

async function openPort() {
  const server = net.createServer()
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address()
  await new Promise((resolve) => server.close(resolve))
  return port
}

async function waitForHealth(url, child, readOutput = () => '') {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (child.exitCode !== null) {
      // Say what the gateway said on the way out. An exit code on its own sent
      // us looking in entirely the wrong place.
      throw new Error(
        `gateway exited early with code ${child.exitCode}.\n` +
          (readOutput().trim() || '(no output)'),
      )
    }
    try {
      const response = await fetch(url)
      if (response.ok) {
        return
      }
    } catch {
      // not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error('gateway did not become healthy')
}

async function startGateway() {
  const port = await openPort()
  const child = spawn(process.execPath, ['src/server.js'], {
    cwd: gatewayRoot,
    env: {
      ...process.env,
      PORT: String(port),
      GATEWAY_AUTH_MODE: 'demo',
      GATEWAY_CSRF_SECRET: 'access-model-test-secret',
      GATEWAY_AUTH_COOKIE_NAME: 'arol_access_test',
      GATEWAY_AUTH_COOKIE_SECURE: 'false',
      GATEWAY_MACHINE_IDS: '',
      MANUALS_DIR: path.join(repoRoot, 'requirements', 'manuals'),
      // No telemetry or business upstream is configured: these tests are about
      // authorization, which must be decided before any upstream is consulted.
      TELEMETRY_SERVICE_URL: '',
      AI_SERVICE_URL: 'http://127.0.0.1:1',
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
    baseUrl: `http://127.0.0.1:${port}`,
    async stop() {
      child.kill('SIGKILL')
      await new Promise((resolve) => child.once('exit', resolve))
    },
  }
}

async function signIn(baseUrl, userId) {
  const response = await fetch(`${baseUrl}/api/v1/auth/demo/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ userId }),
  })
  assert.equal(response.status, 200, `sign-in failed for ${userId}`)
  const cookie = (response.headers.getSetCookie?.() ?? [])[0]?.split(';')[0]
  assert.ok(cookie, 'no session cookie issued')
  return { cookie, session: await response.json() }
}

async function get(baseUrl, pathname, cookie) {
  const response = await fetch(`${baseUrl}${pathname}`, { headers: { Cookie: cookie } })
  let body = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  return { status: response.status, body }
}

/** Assert a refusal is explicit: a real failure with a stated reason. */
function assertDeclined(result, expectedCode, context) {
  assert.equal(result.status, 403, `${context}: expected 403, got ${result.status}`)
  assert.equal(result.body?.code, expectedCode, `${context}: wrong reason code`)
  assert.ok(result.body?.error, `${context}: refusal carried no message`)
  // The failure mode this whole model exists to prevent.
  assert.ok(!Array.isArray(result.body), `${context}: refusal returned a list`)
}

test('the access model gates every domain for every visibility level', async (t) => {
  const gateway = await startGateway()
  t.after(() => gateway.stop())
  const { baseUrl } = gateway

  // --- Sign-in exposes the dataset identities -----------------------------
  const listed = await (await fetch(`${baseUrl}/api/v1/auth/demo/users`)).json()
  assert.equal(listed.users.length, 19)
  assert.deepEqual(
    [...new Set(listed.users.map((user) => user.visibility))].sort(),
    ['commercial', 'full', 'technician'],
  )

  // --- Full visibility ----------------------------------------------------
  const full = await signIn(baseUrl, USERS.full)
  assert.equal(full.session.companyId, 'CMP-001')
  assert.equal(full.session.visibility, 'full')

  for (const build of COMMON_PATHS) {
    const result = await get(baseUrl, build(CMP_001_MACHINE), full.cookie)
    assert.equal(result.status, 200, `full should read ${build(CMP_001_MACHINE)}`)
  }
  // Operational and commercial reads are allowed through authorization; they
  // fail later at the unconfigured upstream, which is a 502 and not a 403.
  for (const build of [...OPERATIONAL_PATHS, ...COMMERCIAL_PATHS]) {
    const result = await get(baseUrl, build(CMP_001_MACHINE), full.cookie)
    assert.notEqual(result.status, 403, `full was denied ${build(CMP_001_MACHINE)}`)
  }

  // --- Technician: no quotes or orders ------------------------------------
  const technician = await signIn(baseUrl, USERS.technician)
  assert.equal(technician.session.visibility, 'technician')

  for (const build of COMMERCIAL_PATHS) {
    const pathname = build(CMP_001_MACHINE)
    assertDeclined(
      await get(baseUrl, pathname, technician.cookie),
      'visibility_denied',
      `technician -> ${pathname}`,
    )
  }
  for (const build of OPERATIONAL_PATHS) {
    const result = await get(baseUrl, build(CMP_001_MACHINE), technician.cookie)
    assert.notEqual(result.status, 403, 'technician must keep operational access')
  }

  // --- Commercial: no telemetry, alarms or maintenance --------------------
  const commercial = await signIn(baseUrl, USERS.commercial)
  assert.equal(commercial.session.visibility, 'commercial')

  for (const build of OPERATIONAL_PATHS) {
    const pathname = build(CMP_001_MACHINE)
    assertDeclined(
      await get(baseUrl, pathname, commercial.cookie),
      'visibility_denied',
      `commercial -> ${pathname}`,
    )
  }

  // Machine identity and documentation stay visible to every user of the
  // owning company, whatever their visibility.
  for (const build of COMMON_PATHS) {
    const result = await get(baseUrl, build(CMP_001_MACHINE), commercial.cookie)
    assert.equal(result.status, 200, `commercial should still read ${build(CMP_001_MACHINE)}`)
  }
})

test('the company boundary is never crossed, at any visibility level', async (t) => {
  const gateway = await startGateway()
  t.after(() => gateway.stop())
  const { baseUrl } = gateway

  for (const userId of [USERS.full, USERS.technician, USERS.commercial]) {
    const { cookie } = await signIn(baseUrl, userId)

    for (const build of [...COMMON_PATHS, ...OPERATIONAL_PATHS, ...COMMERCIAL_PATHS]) {
      const pathname = build(CMP_004_MACHINE)
      assertDeclined(
        await get(baseUrl, pathname, cookie),
        'machine_not_in_company',
        `${userId} -> ${pathname}`,
      )
    }

    // Another company's manual is refused too, even though manuals are common
    // data: common means common to the owning company, not to everyone.
    assertDeclined(
      await get(baseUrl, '/manuals/A2064_manual_EN.pdf', cookie),
      'machine_not_in_company',
      `${userId} -> other company manual`,
    )
  }
})

test('the fleet is scoped to the company, and an empty fleet is not a denial', async (t) => {
  const gateway = await startGateway()
  t.after(() => gateway.stop())
  const { baseUrl } = gateway

  const valgrande = await signIn(baseUrl, USERS.technician)
  const owned = await get(baseUrl, '/api/v1/machines', valgrande.cookie)
  assert.equal(owned.status, 200)
  assert.deepEqual(
    owned.body.machines.map((machine) => machine.id).sort(),
    ['MCH-0001', 'MCH-0002', 'MCH-0003'],
  )
  assert.ok(
    owned.body.machines.every((machine) => machine.companyId === 'CMP-001'),
    'fleet leaked a machine from another company',
  )

  // A company that owns no machines gets an honest empty fleet. That is a true
  // answer about their own data, not a refusal.
  const cascais = await signIn(baseUrl, USERS.otherCompany)
  const empty = await get(baseUrl, '/api/v1/machines', cascais.cookie)
  assert.equal(empty.status, 200)
  assert.deepEqual(empty.body.machines, [])
})

test('machine context withholds domains the user may not see', async (t) => {
  const gateway = await startGateway()
  t.after(() => gateway.stop())
  const { baseUrl } = gateway

  const technician = await signIn(baseUrl, USERS.technician)
  const forTechnician = await get(baseUrl, `/api/v1/machines/${CMP_001_MACHINE}/context`, technician.cookie)
  assert.equal(forTechnician.status, 200)
  assert.equal(forTechnician.body.contract, null)
  assert.deepEqual(
    forTechnician.body.withheld.map((item) => item.domain),
    ['commercial'],
  )

  const commercial = await signIn(baseUrl, USERS.commercial)
  const forCommercial = await get(baseUrl, `/api/v1/machines/${CMP_001_MACHINE}/context`, commercial.cookie)
  assert.equal(forCommercial.status, 200)
  assert.equal(forCommercial.body.telemetry, null)
  // Live health is derived from telemetry and alarms, so it is operational data
  // too and must not leak through the machine card.
  assert.equal(forCommercial.body.machine.status, 'unknown')
  assert.deepEqual(
    forCommercial.body.withheld.map((item) => item.domain),
    ['operational'],
  )

  // The manual is common data and reaches both.
  assert.ok(forTechnician.body.manual.url.endsWith('_manual_EN.pdf'))
  assert.equal(forCommercial.body.manual.url, forTechnician.body.manual.url)
})

test('an unknown user cannot sign in, and an unknown machine is not confused with a denial', async (t) => {
  const gateway = await startGateway()
  t.after(() => gateway.stop())
  const { baseUrl } = gateway

  const rejected = await fetch(`${baseUrl}/api/v1/auth/demo/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ userId: 'USR-999' }),
  })
  assert.equal(rejected.status, 404)
  assert.equal((await rejected.json()).code, 'user_not_found')

  const { cookie } = await signIn(baseUrl, USERS.full)
  const missing = await get(baseUrl, '/api/v1/machines/MCH-9999', cookie)
  assert.equal(missing.status, 404)
  assert.equal(missing.body.code, 'machine_not_found')
})
