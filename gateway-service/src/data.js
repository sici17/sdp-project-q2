/**
 * Machine, manual and telemetry reads for the platform API.
 *
 * Machine identity comes from the converted fleet dataset; manual metadata from
 * the generated manifest, which is derived from the same dataset so that the
 * machine-to-manual join by serial number cannot drift; telemetry and alarms
 * from the telemetry service over authenticated HTTP.
 */

import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { findMachine, listMachines } from './dataset.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const manifestPath = path.resolve(
  process.env.MANUALS_MANIFEST_PATH ??
    path.resolve(__dirname, '..', '..', 'data', 'manuals-manifest.json'),
)
const telemetryServiceUrl = (process.env.TELEMETRY_SERVICE_URL ?? '').replace(/\/+$/, '')
const telemetrySecret = process.env.MCP_SHARED_SECRET ?? ''

/**
 * Raised when an upstream source cannot be reached.
 *
 * Distinguishing "unavailable" from "empty" matters: an operator must never be
 * shown "no alarms" because the telemetry service was down.
 */
export class UpstreamUnavailableError extends Error {
  constructor(message, { source } = {}) {
    super(message)
    this.name = 'UpstreamUnavailableError'
    this.source = source
    this.code = 'upstream_unavailable'
    this.statusCode = 502
  }
}

let manifestCache = null

async function readManifest() {
  if (manifestCache) {
    return manifestCache
  }
  try {
    const raw = await readFile(manifestPath, 'utf8')
    manifestCache = JSON.parse(raw).manuals ?? []
  } catch (error) {
    if (error.code === 'ENOENT') {
      throw new Error(
        `Manual manifest not found at ${manifestPath}. Run 'python scripts/convert_dataset.py'.`,
      )
    }
    throw error
  }
  return manifestCache
}

// -------------------------------------------------------------------------
// Machines
// -------------------------------------------------------------------------

/**
 * Machines for a company, or the whole fleet when `companyId` is null.
 *
 * `includeTelemetry` is false for identities whose visibility excludes
 * operational data: live health is derived from telemetry and alarms, so it is
 * operational data too, however small it looks on a card.
 */
export async function getMachines(requestId, companyId = null, { includeTelemetry = true } = {}) {
  const rows = await listMachines(companyId)
  return Promise.all(
    rows.map(async (row) =>
      machineFromRow(
        row,
        includeTelemetry ? await getLatestTelemetrySafe(row.machineId, requestId) : null,
      ),
    ),
  )
}

export async function getMachine(machineId, requestId, { includeTelemetry = true } = {}) {
  const row = await findMachine(machineId)
  if (!row) {
    return null
  }
  return machineFromRow(
    row,
    includeTelemetry ? await getLatestTelemetrySafe(row.machineId, requestId) : null,
  )
}

/** Machine identity without a telemetry read, for authorization checks. */
export async function getMachineRecord(machineId) {
  return findMachine(machineId)
}

function machineFromRow(row, telemetry) {
  return {
    id: row.machineId,
    companyId: row.companyId,
    serialNumber: row.serialNumber,
    model: row.modelCode,
    modelDescription: row.modelDescription,
    plant: row.plantLocation,
    deliveryDate: row.deliveryDate,
    configurationProfile: row.configurationProfile,
    nominalRateBph: row.nominalRateBph,
    headsCount: row.headsCount,
    supplyVoltage: row.supplyVoltage,
    supplyFrequencyHz: row.supplyFrequencyHz,
    plcFamily: row.plcFamily,
    softwareVersion: row.softwareVersion,
    containerType: row.containerType,
    capType: row.capType,
    status: telemetry?.health ?? 'unknown',
    lastTelemetryAt: telemetry?.timestamp ?? null,
  }
}

// -------------------------------------------------------------------------
// Manuals
// -------------------------------------------------------------------------

export async function getManuals() {
  const entries = await readManifest()
  return entries.map((entry) => manualFromManifest(entry))
}

export async function getManual(machineId) {
  const machine = await findMachine(machineId)
  if (!machine) {
    return null
  }
  const entries = await readManifest()
  const entry = entries.find((item) => item.machineId === machine.machineId)
  if (!entry) {
    return null
  }
  return manualFromManifest(entry, relatedManualsFromManifest(entries, machine.machineId))
}

export async function getManualAccessMachineIds(fileName) {
  let decodedFileName
  try {
    decodedFileName = decodeURIComponent(fileName)
  } catch {
    return null
  }

  const entries = await readManifest()
  const entry = entries.find((item) => {
    if (item.fileName === decodedFileName) {
      return true
    }
    try {
      return path.basename(decodeURIComponent(item.sourceUri ?? '')) === decodedFileName
    } catch {
      return false
    }
  })

  if (!entry) {
    return null
  }

  return [
    ...new Set([
      entry.machineId,
      ...(Array.isArray(entry.relatedMachineIds) ? entry.relatedMachineIds : []),
    ]),
  ]
}

function manualFromManifest(entry, relatedManuals = []) {
  return {
    machineId: entry.machineId,
    title: entry.title,
    version: entry.version ?? null,
    language: entry.language,
    url: entry.sourceUri,
    fileName: entry.fileName,
    serialNumber: entry.serialNumber ?? null,
    // Leading pages before this manual's own page 1. The viewer navigates by
    // the PDF index; without this it also *displayed* the index, so a citation
    // reading "page 82" opened a viewer captioned "89 / 155".
    printedPageOffset: Number(entry.printedPageOffset ?? 0) || 0,
    relatedManuals,
  }
}

function relatedManualsFromManifest(entries, machineId) {
  return entries
    .filter((entry) => Array.isArray(entry.relatedMachineIds) && entry.relatedMachineIds.includes(machineId))
    .map((entry) => manualFromManifest(entry))
}

// -------------------------------------------------------------------------
// Telemetry
// -------------------------------------------------------------------------

export async function getLatestTelemetry(machineId, requestId) {
  return getTelemetryJson(
    `/api/v1/machines/${encodeURIComponent(machineId)}/telemetry/latest`,
    requestId,
  )
}

export async function getMachineTelemetryHistory(machineId, limit = 24, requestId) {
  return getTelemetryJson(
    `/api/v1/machines/${encodeURIComponent(machineId)}/telemetry/history?limit=${encodeURIComponent(limit)}`,
    requestId,
  )
}

export async function getMachineAlarms(machineId, requestId) {
  return getTelemetryJson(`/api/v1/machines/${encodeURIComponent(machineId)}/alarms`, requestId)
}

/**
 * Latest telemetry for list views, where a telemetry outage must not fail the
 * whole request. The machine is still returned, with an unknown status.
 */
async function getLatestTelemetrySafe(machineId, requestId) {
  try {
    return await getLatestTelemetry(machineId, requestId)
  } catch (error) {
    if (error instanceof UpstreamUnavailableError) {
      return null
    }
    throw error
  }
}

async function getTelemetryJson(pathname, requestId) {
  if (!telemetryServiceUrl) {
    throw new UpstreamUnavailableError('Telemetry service is not configured.', {
      source: 'telemetry',
    })
  }

  let response
  try {
    response = await fetch(`${telemetryServiceUrl}${pathname}`, {
      headers: {
        Accept: 'application/json',
        ...(telemetrySecret ? { 'X-Arol-Mcp-Secret': telemetrySecret } : {}),
        ...(requestId ? { 'X-Request-Id': requestId } : {}),
      },
      signal: AbortSignal.timeout(Number(process.env.TELEMETRY_SERVICE_TIMEOUT_MS ?? 3000)),
    })
  } catch (error) {
    throw new UpstreamUnavailableError(`Telemetry service is unreachable: ${error.message}`, {
      source: 'telemetry',
    })
  }

  // A machine the telemetry source does not know is a genuine absence of data,
  // not an outage, so it is reported as such rather than as an error.
  if (response.status === 404) {
    return null
  }

  if (!response.ok) {
    throw new UpstreamUnavailableError(
      `Telemetry service returned ${response.status}.`,
      { source: 'telemetry' },
    )
  }

  return response.json()
}

// -------------------------------------------------------------------------
// Composite context
// -------------------------------------------------------------------------

export async function getMachineContext(machineId, requestId, { includeTelemetry = true } = {}) {
  const machine = await getMachine(machineId, requestId, { includeTelemetry })
  if (!machine) {
    return { machine: null, manual: null, documents: [], telemetry: null }
  }

  const [manual, telemetry] = await Promise.all([
    getManual(machine.id),
    includeTelemetry ? getLatestTelemetrySafe(machine.id, requestId) : null,
  ])

  return {
    machine,
    manual,
    documents: manual ? [{ type: 'manual', title: manual.title, url: manual.url }] : [],
    telemetry,
  }
}
