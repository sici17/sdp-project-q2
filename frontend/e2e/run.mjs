import { spawn } from 'node:child_process'
import { createServer } from 'node:net'
import { setTimeout as delay } from 'node:timers/promises'

/**
 * Refuse to start when something else already holds the port.
 *
 * The exit-code check below was supposed to catch this and could not: it races.
 * Vite's strictPort makes the preview server exit, but the readiness fetch is
 * answered by the squatter - the Compose frontend, typically - and returns 200
 * before the child's exit event is delivered. The suite then ran green against
 * a build that did not contain the change under test. Asking for the port
 * ourselves settles it before anything is spawned.
 */
async function requirePortFree(port) {
  await new Promise((resolve, reject) => {
    const probe = createServer()
    probe.once('error', (error) => {
      reject(
        error.code === 'EADDRINUSE'
          ? new Error(
              `Port ${port} is already in use, so the browser suite would test ` +
                'whatever is serving it instead of this working tree. Stop it ' +
                '(the Compose frontend publishes the same port) and try again.',
            )
          : error,
      )
    })
    probe.once('listening', () => probe.close(() => resolve()))
    probe.listen(port, '127.0.0.1')
  })
}

await requirePortFree(5173)

const server = spawn(process.execPath, ['e2e/server.mjs'], {
  cwd: process.cwd(),
  stdio: 'inherit',
})

let shuttingDown = false

// Whether our own preview server is still alive. Without this the readiness
// check below is satisfied by anything answering on 5173 - the Compose
// frontend, typically - and the browser suite silently tests a different build
// of the app while reporting a pass. It did exactly that.
let serverExited = null
server.once('exit', (code) => {
  serverExited = code ?? 1
})

async function waitForServer() {
  const deadline = Date.now() + 30000

  while (Date.now() < deadline) {
    if (serverExited !== null) {
      throw new Error(
        `The preview server exited with code ${serverExited}. Port 5173 is ` +
          'probably already in use; stop whatever is on it and try again.',
      )
    }

    try {
      const response = await fetch('http://127.0.0.1:5173')
      if (response.ok) {
        return
      }
    } catch {
      // The preview server is still starting.
    }

    await delay(100)
  }

  throw new Error('Timed out waiting for the frontend preview server.')
}

function stopServer() {
  if (!shuttingDown) {
    shuttingDown = true
    server.kill()
  }
}

process.once('SIGINT', () => stopServer())
process.once('SIGTERM', () => stopServer())

try {
  await waitForServer()

  const cliPath = 'node_modules/@playwright/test/cli.js'
  const runner = spawn(process.execPath, [cliPath, 'test', ...process.argv.slice(2)], {
    cwd: process.cwd(),
    stdio: 'inherit',
  })

  const exitCode = await new Promise((resolve) => {
    runner.once('exit', (code) => resolve(code ?? 1))
    runner.once('error', () => resolve(1))
  })

  process.exitCode = exitCode
} finally {
  stopServer()
}
