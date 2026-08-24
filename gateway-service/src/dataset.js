/**
 * Read-only access to the converted AROL Q2 fleet dataset.
 *
 * The workbook is converted once by `scripts/convert_dataset.py` into a small
 * SQLite file that every service opens read-only. Queries here are synchronous:
 * the database is ~1 MB with indexed keys, so each lookup is microseconds, and
 * a synchronous read avoids a connection pool for data that never changes at
 * runtime.
 *
 * Company scoping is expressed as an optional `companyId` on every list query
 * so the tenant boundary can be enforced by the caller without reshaping SQL.
 */

import path from 'node:path'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const datasetPath = path.resolve(
  process.env.DATASET_DB_PATH ?? path.resolve(__dirname, '..', '..', 'data', 'arol_q2.sqlite'),
)

let database = null
let openError = null

async function connect() {
  if (database) {
    return database
  }
  if (openError) {
    throw openError
  }

  if (!existsSync(datasetPath)) {
    openError = new Error(
      `Fleet dataset not found at ${datasetPath}. ` +
        "Run 'python scripts/convert_dataset.py' or set DATASET_DB_PATH.",
    )
    throw openError
  }

  let DatabaseSync
  try {
    ;({ DatabaseSync } = await import('node:sqlite'))
  } catch (error) {
    openError = new Error(
      'Reading the fleet dataset requires node:sqlite. Use Node.js 22.5+ with ' +
        `--experimental-sqlite, or Node.js 24+: ${error.message}`,
    )
    throw openError
  }

  database = new DatabaseSync(datasetPath, { readOnly: true })
  return database
}

async function all(sql, params = []) {
  const db = await connect()
  return db.prepare(sql).all(...params)
}

async function one(sql, params = []) {
  const db = await connect()
  return db.prepare(sql).get(...params) ?? null
}

/** True when the dataset can be opened, used by readiness checks. */
const MACHINE_COLUMNS = `
  m.machineId, m.companyId, m.modelId, m.serialNumber, m.deliveryDate,
  m.plantLocation, m.configurationProfile, m.plcFamily, m.softwareVersion,
  m.manualFile, m.nominalRateBph, m.supplyVoltage, m.supplyFrequencyHz, m.headsCount,
  mm.modelCode, mm.description AS modelDescription, mm.containerType, mm.capType,
  mm.nominalHeads, mm.industrySegment, mm.primitiveDiameter
`

export async function listMachines(companyId = null) {
  return all(
    `SELECT ${MACHINE_COLUMNS}
     FROM Machines m
     JOIN MachineModels mm ON mm.modelId = m.modelId
     ${companyId ? 'WHERE m.companyId = ?' : ''}
     ORDER BY m.machineId;`,
    companyId ? [companyId] : [],
  )
}

/**
 * Resolve a machine by its identifier or its serial number.
 *
 * A QR code on the plant floor may encode either, and an operator reads the
 * serial off the machine plate, so both must reach the same record.
 */
export async function findMachine(identifier) {
  if (typeof identifier !== 'string' || !identifier.trim()) {
    return null
  }
  return one(
    `SELECT ${MACHINE_COLUMNS}
     FROM Machines m
     JOIN MachineModels mm ON mm.modelId = m.modelId
     WHERE m.machineId = ? OR m.serialNumber = ?
     LIMIT 1;`,
    [identifier, identifier],
  )
}

/**
 * Every user in the fleet dataset.
 *
 * All accounts are active, and the demo sign-in offers them as identities. The
 * `visibility` column is the second access check: it narrows which data domains
 * a user reaches inside their own company.
 */
export async function listUsers(companyId = null) {
  return all(
    `SELECT u.userId, u.companyId, u.firstName, u.lastName, u.email, u.jobTitle,
            u.visibility, c.companyName, c.country, c.locale, c.currency
     FROM Users u
     JOIN Companies c ON c.companyId = u.companyId
     ${companyId ? 'WHERE u.companyId = ?' : ''}
     ORDER BY u.companyId, u.userId;`,
    companyId ? [companyId] : [],
  )
}

export async function findUser(identifier) {
  if (typeof identifier !== 'string' || !identifier.trim()) {
    return null
  }
  return one(
    `SELECT u.userId, u.companyId, u.firstName, u.lastName, u.email, u.jobTitle,
            u.visibility, c.companyName, c.country, c.locale, c.currency
     FROM Users u
     JOIN Companies c ON c.companyId = u.companyId
     WHERE u.userId = ? OR lower(u.email) = lower(?)
     LIMIT 1;`,
    [identifier, identifier],
  )
}

/**
 * The dataset's frozen reference date.
 *
 * Open items, overdue work and expiry dates are judged against this rather than
 * the wall clock, because the supplied data covers a fixed window.
 */
let cachedToday = null
export async function platformToday() {
  if (process.env.PLATFORM_TODAY) {
    return process.env.PLATFORM_TODAY
  }
  if (cachedToday) {
    return cachedToday
  }
  const row = await one("SELECT value FROM dataset_meta WHERE key = 'platformToday';")
  cachedToday = row?.value ?? '2026-08-05'
  return cachedToday
}

// -------------------------------------------------------------------------
// Commercial: quotes, revisions and orders
// -------------------------------------------------------------------------

/**
 * Quotes issued to a company, each annotated with its current revision.
 *
 * A quote carries no status of its own: the lifecycle lives on its revisions,
 * and the highest revisionNumber is the current one.
 */
export async function listQuotes(companyId) {
  const today = await platformToday()
  return all(
    `SELECT q.quoteId, q.companyId, q.currency, q.createdAt, q.validUntil, q.description,
            r.quoteRevisionId AS currentRevisionId,
            r.revisionNumber  AS currentRevisionNumber,
            r.revisionStatus  AS currentRevisionStatus,
            r.issuedAt        AS currentRevisionIssuedAt,
            r.discountRate    AS currentDiscountRate,
            r.changeSummary   AS currentChangeSummary,
            (SELECT COUNT(*) FROM QuoteRevisions x WHERE x.quoteId = q.quoteId) AS revisionCount,
            (SELECT ROUND(SUM(l.price), 2) FROM QuoteLines l
              WHERE l.quoteRevisionId = r.quoteRevisionId) AS currentTotal,
            CASE WHEN date(q.validUntil) < date(?) THEN 1 ELSE 0 END AS expired
     FROM Quotes q
     JOIN current_quote_revision r ON r.quoteId = q.quoteId
     WHERE q.companyId = ?
     ORDER BY q.createdAt DESC;`,
    [today, companyId],
  )
}

/**
 * Orders for a company, with content taken from the approved quote revision.
 *
 * OrderLines tracks fulfilment only; it carries no item, quantity or price.
 */
export async function listOrders(companyId, machineId = null) {
  const orders = machineId
    ? await all(
        `SELECT DISTINCT o.* FROM Orders o
         JOIN QuoteRevisions r ON r.quoteId = o.quoteId AND r.revisionStatus = 'Approved'
         JOIN QuoteLines l ON l.quoteRevisionId = r.quoteRevisionId
         WHERE o.companyId = ? AND l.machineId = ?
         ORDER BY o.orderDate DESC;`,
        [companyId, machineId],
      )
    : await all('SELECT * FROM Orders WHERE companyId = ? ORDER BY orderDate DESC;', [companyId])

  const today = await platformToday()
  for (const order of orders) {
    order.lines = await all(
      `SELECT l.quoteLineId, l.machineId, l.price, l.description
       FROM QuoteRevisions r
       JOIN QuoteLines l ON l.quoteRevisionId = r.quoteRevisionId
       WHERE r.quoteId = ? AND r.revisionStatus = 'Approved'
       ORDER BY l.quoteLineId;`,
      [order.quoteId],
    )
    order.fulfillment = await all(
      'SELECT orderLineId, fulfillmentStatus FROM OrderLines WHERE orderId = ? ORDER BY orderLineId;',
      [order.orderId],
    )
    order.total = Number(order.lines.reduce((sum, line) => sum + (line.price ?? 0), 0).toFixed(2))
    // The dataset contains an order placed after its quote had lapsed. Flagging
    // it is more useful than hiding it.
    const quote = await one('SELECT validUntil FROM Quotes WHERE quoteId = ?;', [order.quoteId])
    order.orderedAfterQuoteExpiry = Boolean(
      quote?.validUntil && order.orderDate > quote.validUntil,
    )
    order.asOf = today
  }
  return orders
}

// -------------------------------------------------------------------------
// Operational: alarms and maintenance tickets
// -------------------------------------------------------------------------

/**
 * Maintenance tickets for a machine, joined to the alarm that raised them.
 *
 * MaintenanceTickets.alarmId is empty for tickets that did not originate from
 * an alarm, so this is a left join by design.
 */
export async function listMaintenanceTickets(machineId) {
  return all(
    `SELECT t.ticketId, t.machineId, t.alarmId, t.ticketType, t.ticketStatus,
            t.priority, t.createdDate, t.ownerRole,
            a.alarmCode, a.severity AS alarmSeverity
     FROM MaintenanceTickets t
     LEFT JOIN Alarms a ON a.alarmId = t.alarmId
     WHERE t.machineId = ?
     ORDER BY t.createdDate DESC;`,
    [machineId],
  )
}
