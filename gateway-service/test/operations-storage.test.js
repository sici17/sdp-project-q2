import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { buildOperationsStore } from '../src/operations-store.js'

test('sqlite operations storage persists events and sessions across store instances', async (t) => {
  const operationsDir = await mkdtemp(path.join(os.tmpdir(), 'arol-q2-sqlite-'))
  let store
  let reopened
  t.after(async () => {
    store?.close()
    reopened?.close()
    await rm(operationsDir, { recursive: true, force: true })
  })

  const paths = storagePaths(operationsDir)
  store = await buildOperationsStore(paths)
  await store.appendEvent(
    'audit',
    {
      id: 'audit-1',
      timestamp: '2026-07-11T10:00:00.000Z',
      machineId: 'machine-1',
      type: 'chat.completed',
    },
    365,
  )
  await store.createAuthSession({
    sessionId: 'session-1',
    subject: 'operator-1',
    accessToken: 'server-only-access-token',
    refreshToken: 'server-only-refresh-token',
    accessExpiresAt: '2026-07-11T11:00:00.000Z',
    expiresAt: '2026-07-12T10:00:00.000Z',
    claims: { sub: 'operator-1' },
  })
  await store.createOidcLoginState({
    state: 'state-1',
    nonce: 'nonce-1',
    codeVerifier: 'code-verifier-1',
    returnTo: '/m/euro-vp-2019-01',
    expiresAt: '2099-07-11T10:00:00.000Z',
  })
  await store.createOidcLogoutState({
    state: 'logout-state-1',
    idToken: 'server-only-id-token',
    returnTo: '/m/euro-vp-2019-01',
    expiresAt: '2099-07-11T10:00:00.000Z',
  })

  reopened = await buildOperationsStore(paths)
  assert.deepEqual((await reopened.listEvents('audit', 10)).map((event) => event.id), ['audit-1'])
  assert.equal((await reopened.getAuthSession('session-1')).accessToken, 'server-only-access-token')
  assert.equal((await reopened.consumeOidcLoginState('state-1')).codeVerifier, 'code-verifier-1')
  assert.equal(await reopened.consumeOidcLoginState('state-1'), null)
  assert.equal((await reopened.getOidcLogoutState('logout-state-1')).idToken, 'server-only-id-token')
  assert.equal((await reopened.consumeOidcLogoutState('logout-state-1')).returnTo, '/m/euro-vp-2019-01')
  assert.equal(await reopened.consumeOidcLogoutState('logout-state-1'), null)
})

function storagePaths(operationsDir) {
  return {
    operationsDir,
    databasePath: path.join(operationsDir, 'operations.sqlite'),
  }
}
