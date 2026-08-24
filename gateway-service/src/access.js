/**
 * The dataset's access model, in one place.
 *
 * Two independent checks must both pass before any data is returned:
 *
 *  1. `companyId` is the tenant boundary. A user only ever reaches rows
 *     belonging to their own company. This is never crossed, at any visibility
 *     level.
 *  2. `visibility` narrows *what kind* of data the user sees inside their own
 *     company.
 *
 * A request that falls outside a user's scope is declined explicitly. It is
 * never answered from another company's data, and never returned as an empty
 * result as though no data existed: "you may not see this" and "there is
 * nothing here" are different answers and an operator must be able to tell
 * them apart.
 */

import { HttpError } from './http-error.js'

/**
 * The three domains the data splits into.
 *
 * Machine identity and documentation is visible to every user of the owning
 * company; the other two are restricted by visibility.
 */
export const DOMAIN = {
  /** Machines, MachineModels, and the machine manuals. */
  COMMON: 'common',
  /** TelemetrySnapshots, Alarms, MaintenanceTickets. */
  OPERATIONAL: 'operational',
  /** Quotes, QuoteRevisions, QuoteLines, Orders, OrderLines. */
  COMMERCIAL: 'commercial',
}

export const VISIBILITY = {
  FULL: 'full',
  TECHNICIAN: 'technician',
  COMMERCIAL: 'commercial',
}

const DOMAINS_BY_VISIBILITY = new Map([
  [VISIBILITY.FULL, new Set([DOMAIN.COMMON, DOMAIN.OPERATIONAL, DOMAIN.COMMERCIAL])],
  [VISIBILITY.TECHNICIAN, new Set([DOMAIN.COMMON, DOMAIN.OPERATIONAL])],
  [VISIBILITY.COMMERCIAL, new Set([DOMAIN.COMMON, DOMAIN.COMMERCIAL])],
])

const DOMAIN_LABEL = {
  [DOMAIN.OPERATIONAL]: 'telemetry, alarms and maintenance data',
  [DOMAIN.COMMERCIAL]: 'quotes and orders',
  [DOMAIN.COMMON]: 'machine and manual data',
}

export function isKnownVisibility(value) {
  return DOMAINS_BY_VISIBILITY.has(value)
}

export function domainsFor(visibility) {
  return [...(DOMAINS_BY_VISIBILITY.get(visibility) ?? [])]
}

/**
 * True when this identity is AROL staff rather than a customer.
 *
 * Support crosses the tenant boundary by design: it serves every customer.
 * Customer identities never do.
 */
function isStaff(authContext) {
  return Boolean(authContext?.isAdmin)
}

/**
 * Local development escape hatch. `GATEWAY_AUTH_MODE=off` disables
 * authentication entirely, so there is no identity to check against.
 */
function isAuthDisabled(authContext) {
  return authContext?.mode === 'off'
}

/**
 * Check 1: the tenant boundary.
 *
 * @param {object} authContext
 * @param {{machineId: string, companyId: string}|null} machine
 */
export function assertTenancy(authContext, machine) {
  if (!machine) {
    throw new HttpError(404, 'Machine not found.', { code: 'machine_not_found' })
  }

  if (isAuthDisabled(authContext) || isStaff(authContext)) {
    return
  }

  const companyId = authContext?.companyId
  if (!companyId) {
    // Fail closed. An identity with no company cannot be inside any tenant, so
    // there is no correct set of rows to return.
    throw new HttpError(403, 'This identity is not associated with a company.', {
      code: 'company_unknown',
    })
  }

  if (machine.companyId !== companyId) {
    // Deliberately explicit rather than a 404: pretending the machine does not
    // exist would be a different lie, and the operator scanned a real machine.
    throw new HttpError(
      403,
      'This machine belongs to another company and is not in your fleet.',
      { code: 'machine_not_in_company' },
    )
  }
}

/**
 * Check 2: the visibility domain.
 *
 * @param {object} authContext
 * @param {string} domain one of DOMAIN
 */
export function assertVisibility(authContext, domain) {
  if (isAuthDisabled(authContext) || isStaff(authContext)) {
    return
  }

  const visibility = authContext?.visibility
  if (!isKnownVisibility(visibility)) {
    throw new HttpError(403, 'This identity has no data visibility assigned.', {
      code: 'visibility_unknown',
    })
  }

  if (DOMAINS_BY_VISIBILITY.get(visibility).has(domain)) {
    return
  }

  throw new HttpError(
    403,
    `Your ${visibility} access does not include ${DOMAIN_LABEL[domain] ?? domain}.`,
    { code: 'visibility_denied', domain, visibility },
  )
}

/** True when the identity may read this domain, without throwing. */
export function canSeeDomain(authContext, domain) {
  try {
    assertVisibility(authContext, domain)
    return true
  } catch {
    return false
  }
}

/** True when the identity may read this machine, without throwing. */
export function canSeeMachine(authContext, machine) {
  try {
    assertTenancy(authContext, machine)
    return true
  } catch {
    return false
  }
}

/**
 * The company whose rows this identity may read, or null for staff and for
 * disabled auth, which are not restricted to one tenant.
 */
export function tenantCompanyId(authContext) {
  if (isAuthDisabled(authContext) || isStaff(authContext)) {
    return null
  }
  return authContext?.companyId ?? null
}
