import { mkdir } from 'node:fs/promises'
import path from 'node:path'

const EVENT_TABLES = {
  audit: 'audit_events',
  alert: 'alert_events',
}

export async function buildOperationsStore({ operationsDir, databasePath }) {
  return SqliteOperationsStore.create({ operationsDir, databasePath })
}

class SqliteOperationsStore {
  static async create({ operationsDir, databasePath }) {
    let DatabaseSync
    try {
      ;({ DatabaseSync } = await import('node:sqlite'))
    } catch (error) {
      throw new Error(
        `SQLite operations storage requires Node.js with node:sqlite support: ${error.message}`,
      )
    }

    await mkdir(operationsDir, { recursive: true })
    await mkdir(path.dirname(databasePath), { recursive: true })
    const store = new SqliteOperationsStore(new DatabaseSync(databasePath))
    store.initialize()
    return store
  }

  constructor(db) {
    this.db = db
    this.mode = 'sqlite'
  }

  initialize() {
    this.db.exec(`
      PRAGMA busy_timeout = 5000;
      PRAGMA journal_mode = WAL;
      CREATE TABLE IF NOT EXISTS audit_events (
        id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        machine_id TEXT,
        payload TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS audit_events_timestamp_idx ON audit_events(timestamp DESC);
      CREATE TABLE IF NOT EXISTS alert_events (
        id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        machine_id TEXT,
        payload TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS alert_events_timestamp_idx ON alert_events(timestamp DESC);
      CREATE TABLE IF NOT EXISTS auth_sessions (
        session_id TEXT PRIMARY KEY,
        expires_at TEXT NOT NULL,
        payload TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS auth_sessions_expires_idx ON auth_sessions(expires_at);
      CREATE TABLE IF NOT EXISTS oidc_login_states (
        state TEXT PRIMARY KEY,
        expires_at TEXT NOT NULL,
        payload TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS oidc_login_states_expires_idx ON oidc_login_states(expires_at);
      CREATE TABLE IF NOT EXISTS oidc_logout_states (
        state TEXT PRIMARY KEY,
        expires_at TEXT NOT NULL,
        payload TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS oidc_logout_states_expires_idx ON oidc_logout_states(expires_at);
    `)
  }

  async readiness() {
    this.db.prepare('SELECT 1').get()
    return { status: 'ready', required: true, persistence: this.mode }
  }

  async appendEvent(kind, entry, retentionDays) {
    const table = EVENT_TABLES[kind]
    const result = this.db
      .prepare(
        `INSERT OR IGNORE INTO ${table} (id, timestamp, machine_id, payload) VALUES (?, ?, ?, ?)`,
      )
      .run(entry.id, entry.timestamp, entry.machineId ?? null, JSON.stringify(entry))

    if (retentionDays > 0) {
      const cutoff = new Date(Date.now() - retentionDays * 24 * 60 * 60 * 1000).toISOString()
      this.db.prepare(`DELETE FROM ${table} WHERE timestamp < ?`).run(cutoff)
    }
    return result.changes > 0
  }

  async listEvents(kind, limit) {
    const table = EVENT_TABLES[kind]
    const rows = this.db
      .prepare(`SELECT payload FROM ${table} ORDER BY timestamp DESC LIMIT ?`)
      .all(Math.min(Math.max(Number(limit) || 1, 1), 1_000_000))
    return rows.map((row) => JSON.parse(row.payload))
  }

  async createAuthSession(session) {
    this.db
      .prepare(
        `INSERT OR REPLACE INTO auth_sessions (session_id, expires_at, payload)
         VALUES (?, ?, ?)`,
      )
      .run(session.sessionId, session.expiresAt, JSON.stringify(session))
  }

  async getAuthSession(sessionId) {
    const row = this.db
      .prepare('SELECT payload FROM auth_sessions WHERE session_id = ?')
      .get(sessionId)
    return row ? JSON.parse(row.payload) : null
  }

  async deleteAuthSession(sessionId) {
    this.db.prepare('DELETE FROM auth_sessions WHERE session_id = ?').run(sessionId)
  }

  async createOidcLoginState(loginState) {
    this.db
      .prepare(
        `INSERT OR REPLACE INTO oidc_login_states (state, expires_at, payload)
         VALUES (?, ?, ?)`,
      )
      .run(loginState.state, loginState.expiresAt, JSON.stringify(loginState))
  }

  async consumeOidcLoginState(stateValue) {
    const now = new Date().toISOString()
    this.db.exec('BEGIN IMMEDIATE')
    try {
      this.db.prepare('DELETE FROM oidc_login_states WHERE expires_at <= ?').run(now)
      const row = this.db
        .prepare('SELECT payload FROM oidc_login_states WHERE state = ? AND expires_at > ?')
        .get(stateValue, now)
      this.db.prepare('DELETE FROM oidc_login_states WHERE state = ?').run(stateValue)
      this.db.exec('COMMIT')
      return row ? JSON.parse(row.payload) : null
    } catch (error) {
      this.db.exec('ROLLBACK')
      throw error
    }
  }

  async createOidcLogoutState(logoutState) {
    this.db
      .prepare(
        `INSERT OR REPLACE INTO oidc_logout_states (state, expires_at, payload)
         VALUES (?, ?, ?)`,
      )
      .run(logoutState.state, logoutState.expiresAt, JSON.stringify(logoutState))
  }

  async getOidcLogoutState(stateValue) {
    const now = new Date().toISOString()
    const row = this.db
      .prepare('SELECT payload FROM oidc_logout_states WHERE state = ? AND expires_at > ?')
      .get(stateValue, now)
    return row ? JSON.parse(row.payload) : null
  }

  async consumeOidcLogoutState(stateValue) {
    const now = new Date().toISOString()
    this.db.exec('BEGIN IMMEDIATE')
    try {
      this.db.prepare('DELETE FROM oidc_logout_states WHERE expires_at <= ?').run(now)
      const row = this.db
        .prepare('SELECT payload FROM oidc_logout_states WHERE state = ? AND expires_at > ?')
        .get(stateValue, now)
      this.db.prepare('DELETE FROM oidc_logout_states WHERE state = ?').run(stateValue)
      this.db.exec('COMMIT')
      return row ? JSON.parse(row.payload) : null
    } catch (error) {
      this.db.exec('ROLLBACK')
      throw error
    }
  }

  close() {
    this.db.close()
  }
}
