import { preview } from 'vite'

// strictPort matters more than it looks. Without it Vite moves to the next
// free port, the runner's readiness check succeeds against whatever else is
// already on 5173 - the Compose frontend, typically - and the whole browser
// suite silently tests a different build. It did exactly that, and passed.
const server = await preview({
  preview: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
})

async function closeServer() {
  await server.close()
  process.exit(0)
}

process.once('SIGINT', () => void closeServer())
process.once('SIGTERM', () => void closeServer())
