export type MachineStatus = 'healthy' | 'warning' | 'critical' | 'offline'

export type ChatRole = 'user' | 'assistant' | 'system' | 'tool'

export type AgentName =
  | 'supervisor'
  | 'doc-agent'
  | 'telemetry-agent'
  | 'troubleshooting-agent'
  | 'business-agent'

export type ToolCallStatus = 'ok' | 'empty' | 'error'

export type ActionPriority = 'immediate' | 'next' | 'escalate'

export interface Machine {
  id: string
  serialNumber: string
  model: string
  plant: string
  status: MachineStatus
  lastTelemetryAt?: string
}

export interface ManualReference {
  machineId: string
  title: string
  version: string
  language: string
  url: string
}

export interface Manual extends ManualReference {
  relatedManuals?: ManualReference[]
  /** Leading pages before this manual's own page 1; 0 when they agree. */
  printedPageOffset?: number
}

export interface ManualIndexEntry {
  machineId: string
  title: string
  version: string
  language: string
  fileName: string
  sourceUri: string
  relatedMachineIds?: string[]
  pdfExists: boolean
  indexedChunkCount: number | null
  status: 'indexed' | 'unindexed' | 'unknown'
}

export interface ManualIndexStatus {
  ragEnabled: boolean
  collection: string
  embeddingModel: string
  status: 'indexed' | 'empty' | 'unavailable'
  totalIndexedChunks: number
  error?: string
  manuals: ManualIndexEntry[]
}

export interface ManualSearchResponse {
  machineId: string
  query: string
  count: number
  evidence: Evidence[]
}


/**
 * One hourly reading, in the shape the fleet dataset actually publishes.
 *
 * There is no RPM and no torque here, because the dataset records neither. The
 * console used to show both as headline tiles, which meant two of its four
 * primary readings said "Unavailable" on every machine forever, while the
 * figures that do exist — how fast it is running against its own nominal rate,
 * how much of the hour it produced, how many alarms it raised — were not shown
 * at all.
 */
export interface TelemetrySnapshot {
  machineId: string
  timestamp: string
  operationalStatus?: string | null
  /** Bottles per hour in this interval; 0 whenever the machine is not producing. */
  productionRateBph?: number | null
  /** This machine's own rated speed. Two machines of a model can differ. */
  nominalRateBph?: number | null
  /** productionRateBph as a share of nominalRateBph, which is the only sound comparison. */
  rateUtilizationPct?: number | null
  uptimePercentage?: number | null
  alarmCount?: number | null
  temperatureC?: number | null
  energyKwh?: number | null
  healthNote?: string | null
  activeAlarm: string | null
  health: MachineStatus
  source?: string
  quality?: 'fresh' | 'stale' | 'partial' | 'invalid' | 'invalid-timestamp' | string
  ageSeconds?: number
  staleAfterSeconds?: number
  missingFields?: string[]
}

/**
 * A machine's service standing.
 *
 * There is no warranty status or SLA here because the fleet dataset holds
 * neither. `coverageNote` says where a coverage answer actually lives, which is
 * more useful than a field rendering "Unavailable" next to the word Warranty.
 */
export interface ServiceContract {
  machineId: string
  companyId?: string | null
  serialNumber?: string | null
  modelCode?: string | null
  deliveryDate?: string | null
  plantLocation?: string | null
  acquisitionCost?: number | null
  currency?: string | null
  openTicketCount?: number
  ticketCount?: number
  lastScheduledMaintenance?: string | null
  coverageNote?: string
}

export interface MachineContext {
  machine: Machine
  manual: Manual | null
  telemetry: TelemetrySnapshot | null
  contract: ServiceContract | null
}

export interface AuthSession {
  mode: 'off' | 'oidc' | string
  subject: string
  roles: string[]
  machineIds: string[]
  hasMachineRestriction: boolean
  authenticated?: boolean
  logoutRequired?: boolean
  expiresInSeconds?: number
  csrfToken?: string
  /** Identity metadata only; every access decision is made server-side. */
  userId?: string | null
  companyId?: string | null
  visibility?: string | null
  /** The data domains this identity may read. */
  domains?: string[]
}

export type ChatAttachmentType =
  | 'application/pdf'
  | 'text/plain'

export interface ChatAttachment {
  id: string
  name: string
  type: ChatAttachmentType
  size: number
  contentBase64: string
}

export type ChatAttachmentSummary = Pick<ChatAttachment, 'id' | 'name' | 'type' | 'size'>

export interface ChatMessage {
  id: string
  role: ChatRole
  content: string
  createdAt: string
  attachments?: ChatAttachmentSummary[]
}

export interface ChatSession {
  sessionId: string
  machineId: string
  ownerSubject?: string
  createdAt?: string
  expiresAt?: string
  messageCount?: number
  messages: ChatMessage[]
  diagnosticState?: Record<string, unknown> | null
}

export interface Evidence {
  source: string
  title: string
  excerpt: string
  /** PDF index; what the viewer navigates by. */
  page?: number
  /** The number printed on that page of the manual, when it differs. */
  printedPage?: number
  confidence?: number
  manualVersion?: string
  language?: string
  sourceUri?: string
  chunkId?: string
  section?: string
  score?: number
  chunkKind?: 'procedure' | 'troubleshooting' | 'safety' | 'reference' | 'table'
  topics?: string[]
  alarmCodes?: string[]
  safetyLevel?: 'operator' | 'technician' | 'safety-critical'
}

export interface ToolCallRecord {
  id: string
  name: string
  agent: AgentName
  status: ToolCallStatus
  inputSummary: string
  outputSummary: string
}

export interface RecommendedAction {
  label: string
  priority: ActionPriority
  requiresTechnician: boolean
  source: string
}

export interface DiagnosticStep {
  label: string
  detail: string
  priority: ActionPriority
  requiresTechnician: boolean
  source: string
  evidenceTitle?: string
  /** PDF index; what the viewer navigates by. */
  page?: number
  /** The number printed on that page of the manual, when it differs. */
  printedPage?: number
  expectedOutcome?: string
  passFollowUp?: string
  failFollowUp?: string
  safetyLevel?: 'operator' | 'technician' | 'safety-critical'
  requiredRole?: string
  stepId?: string
  passNextStepId?: string
  failNextStepId?: string
  status?: 'pending' | 'completed' | 'failed'
}

export interface ChatResponse {
  message: ChatMessage
  agentTrace: AgentName[]
  intents: string[]
  routingReason: string
  evidence: Evidence[]
  toolCalls: ToolCallRecord[]
  diagnosticSteps: DiagnosticStep[]
  recommendedActions: RecommendedAction[]
  answerConfidence: number
  reviewRequired: boolean
  reviewReasons: string[]
}

/** A dataset identity offered by the demo sign-in. */
export interface DemoUser {
  userId: string
  name: string
  email: string
  jobTitle: string
  visibility: 'full' | 'technician' | 'commercial' | string
  companyId: string
  companyName: string
  country: string
  /** The data domains this identity will be able to read. */
  domains: string[]
}

/** A machine as it appears in the fleet list. */
export interface MachineSummary {
  id: string
  companyId: string
  serialNumber: string
  model: string
  modelDescription?: string | null
  plant: string
  deliveryDate?: string | null
  configurationProfile?: string | null
  nominalRateBph?: number | null
  headsCount?: number | null
  plcFamily?: string | null
  softwareVersion?: string | null
  /** 'unknown' when the caller may not see operational data. */
  status: string
  lastTelemetryAt?: string | null
}

export interface QuoteSummary {
  quoteId: string
  companyId: string
  description: string | null
  currency: string | null
  createdAt: string | null
  validUntil: string | null
  currentRevisionNumber: number
  /** The lifecycle lives on the revision, not on the quote. */
  currentRevisionStatus: string
  currentTotal: number | null
  revisionCount: number
  currentDiscountRate: number | null
  currentChangeSummary: string | null
  /** Judged against the dataset's frozen reference date. */
  expired: number | boolean
}

export interface QuoteLine {
  quoteLineId: string
  /** Empty on lines that do not refer to an installed machine. */
  machineId: string | null
  price: number | null
  description: string
}

export interface OrderRecord {
  orderId: string
  quoteId: string
  companyId: string
  orderStatus: string
  shipmentStatus: string
  orderDate: string
  expectedDeliveryDate: string | null
  currency: string | null
  notes: string | null
  /** Content comes from the approved revision of the quote. */
  lines: QuoteLine[]
  /** Order lines carry fulfilment only. */
  fulfillment: { orderLineId: string; fulfillmentStatus: string }[]
  total: number
  orderedAfterQuoteExpiry: boolean
}

export interface MaintenanceTicket {
  ticketId: string
  machineId: string
  /** Empty on tickets that did not originate from an alarm. */
  alarmId: string | null
  ticketType: string
  ticketStatus: string
  priority: string
  createdDate: string
  ownerRole: string
  alarmCode: string | null
  alarmSeverity: string | null
}

