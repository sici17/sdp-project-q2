import CloseIcon from '@mui/icons-material/Close'
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import Alert from '@mui/material/Alert'
import AppBar from '@mui/material/AppBar'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Card from '@mui/material/Card'
import Chip from '@mui/material/Chip'
import Drawer from '@mui/material/Drawer'
import IconButton from '@mui/material/IconButton'
import Stack from '@mui/material/Stack'
import Toolbar from '@mui/material/Toolbar'
import Typography from '@mui/material/Typography'

import { AccountDetails, AccountMenu } from './components/AccountMenu'
import { AppRail } from './components/AppRail'
import { ChatPane } from './components/ChatPane'
import { EvidenceDialog } from './components/EvidenceDialog'
import { MachineContextPanel } from './components/MachineContextPanel'
import type { ContextSection } from './components/MachineContextPanel'
import { ManualPane } from './components/ManualPane'
import { ManualSheet } from './components/ManualSheet'
import { MobileWorkspace } from './components/MobileWorkspace'
import { M3, MONO, MOTION, displayLabel, statusSeverity } from './theme'

/** The machine-context column, at the width the design system specifies. */
const PANEL_WIDTH = 300
import {
  createChatSession,
  GatewayRequestError,
  describeGatewayError,
  getAuthSession,
  getChatSession,
  getGatewayAssetUrl,
  getMachineContext,
  getManualIndexStatus,
  listDemoUsers,
  searchManualIndex,
  logoutAuthSession,
  startProviderLogout,
  startOidcLogin,
  streamChatMessage,
} from './services/api'
import { sanitizeEvidence } from './evidence'
import { createClientId } from './clientId'
import type {
  AuthSession,
  ChatAttachment,
  ChatAttachmentSummary,
  ChatMessage,
  ChatSession,
  DemoUser,
  DiagnosticStep,
  Evidence,
  Machine,
  Manual,
  ManualIndexStatus,
  ServiceContract,
  TelemetrySnapshot,
} from './types/contracts'

type MessageEvidence = Record<string, Evidence[]>

interface RetryableSubmission {
  message: string
  attachments: ChatAttachment[]
  idempotencyKey: string
  userMessageId: string
  assistantMessageId: string
}

const initialAssistantMessage: ChatMessage = {
  id: 'welcome',
  role: 'assistant',
  content:
    'Ask about this machine, its manual, alarms, maintenance history or service records. ' +
    'Relevant manual pages and records are included with each answer.',
  createdAt: new Date().toISOString(),
}

/*
  Openers that this dataset can actually answer. They used to ask about a torque
  alarm and "simulator telemetry", neither of which exists here: the alarms are
  ALnnn_MNEMONIC codes and the telemetry is the supplied fleet stream. Offering
  a question the data cannot answer is a poor first impression of a system whose
  whole claim is that it answers from the data.
*/
const quickPrompts = [
  'Why is this machine raising repeated alarms?',
  'What maintenance was recently performed?',
  'Which safety procedures apply before maintenance?',
]


function getPageFromUrl() {
  const params = new URLSearchParams(window.location.search)
  const page = Number(params.get('page'))

  return Number.isInteger(page) && page > 0 ? page : null
}

function getSectionFromUrl() {
  const params = new URLSearchParams(window.location.search)
  const section = params.get('section')?.trim()

  return section || null
}

function isCompactViewport() {
  return typeof window !== 'undefined' && window.matchMedia('(max-width: 680px)').matches
}

function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() =>
    typeof window === 'undefined' ? false : window.matchMedia(query).matches,
  )

  useEffect(() => {
    const media = window.matchMedia(query)
    const update = () => setMatches(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [query])

  return matches
}

function chatSessionKey(machineId: string) {
  return `arol-q2:chat-session:${machineId}`
}

function chatEvidenceKey(sessionId: string) {
  return `arol-q2:chat-evidence:${sessionId}`
}

function chatStepsKey(sessionId: string) {
  return `arol-q2:chat-steps:${sessionId}`
}

/**
 * Keep the checklist across a reload.
 *
 * The steps arrived only on a live response, so refreshing - or a phone locking
 * itself mid-procedure - restored the conversation but dropped the checklist and
 * every step already ticked off. On a safety checklist that means losing your
 * place with no way to tell which checks you had done.
 */
function rememberDiagnostics(
  sessionId: string,
  steps: DiagnosticStep[],
  checks: Record<string, boolean>,
) {
  try {
    window.sessionStorage.setItem(chatStepsKey(sessionId), JSON.stringify({ steps, checks }))
  } catch {
    // Same contract as the evidence store: a convenience, never a requirement.
  }
}

function readStoredDiagnostics(sessionId: string): {
  steps: DiagnosticStep[]
  checks: Record<string, boolean>
} {
  try {
    const stored = window.sessionStorage.getItem(chatStepsKey(sessionId))
    if (!stored) {
      return { steps: [], checks: {} }
    }

    const parsed = JSON.parse(stored) as {
      steps?: unknown
      checks?: unknown
    }
    return {
      steps: Array.isArray(parsed.steps) ? (parsed.steps as DiagnosticStep[]) : [],
      checks:
        parsed.checks && typeof parsed.checks === 'object'
          ? (parsed.checks as Record<string, boolean>)
          : {},
    }
  } catch {
    return { steps: [], checks: {} }
  }
}

function readStoredSessionId(machineId: string) {
  try {
    return window.sessionStorage.getItem(chatSessionKey(machineId))
  } catch {
    return null
  }
}

function rememberSession(machineId: string, sessionId: string) {
  try {
    window.sessionStorage.setItem(chatSessionKey(machineId), sessionId)
  } catch {
    // Storage can be unavailable in privacy modes; the server session still works for this page.
  }
}

function forgetSession(machineId: string) {
  try {
    window.sessionStorage.removeItem(chatSessionKey(machineId))
  } catch {
    // Ignore unavailable browser storage.
  }
}

function readStoredEvidence(sessionId: string): MessageEvidence {
  try {
    const stored = window.sessionStorage.getItem(chatEvidenceKey(sessionId))
    if (!stored) {
      return {}
    }

    const parsed = JSON.parse(stored) as Record<string, unknown>
    return Object.fromEntries(
      Object.entries(parsed).flatMap(([messageId, items]) => {
        if (!Array.isArray(items)) {
          return []
        }
        const sanitized = sanitizeEvidence(items as Evidence[])
        return sanitized.length > 0 ? [[messageId, sanitized]] : []
      }),
    )
  } catch {
    return {}
  }
}

function rememberEvidence(sessionId: string, messageEvidence: MessageEvidence) {
  try {
    const recentEntries = Object.entries(messageEvidence)
      .map(([messageId, items]) => [messageId, sanitizeEvidence(items)] as const)
      .filter(([, items]) => items.length > 0)
      .slice(-40)
    window.sessionStorage.setItem(chatEvidenceKey(sessionId), JSON.stringify(Object.fromEntries(recentEntries)))
  } catch {
    // Evidence persistence is a convenience; chat remains usable without browser storage.
  }
}

function clearStoredChatState() {
  try {
    for (let index = window.sessionStorage.length - 1; index >= 0; index -= 1) {
      const key = window.sessionStorage.key(index)
      if (key?.startsWith('arol-q2:chat-')) {
        window.sessionStorage.removeItem(key)
      }
    }
  } catch {
    // Ignore unavailable browser storage while completing server-side logout.
  }
}

async function resumeOrCreateChatSession(machineId: string): Promise<ChatSession> {
  const storedSessionId = readStoredSessionId(machineId)

  if (storedSessionId) {
    try {
      const session = await getChatSession(storedSessionId)
      if (session.machineId === machineId) {
        return session
      }
      forgetSession(machineId)
    } catch (error) {
      if (
        error instanceof GatewayRequestError &&
        [403, 404, 410].includes(error.status)
      ) {
        forgetSession(machineId)
      } else {
        throw error
      }
    }
  }

  const idempotencyKey = createClientId()
  let session: ChatSession

  try {
    session = await createChatSession(machineId, idempotencyKey)
  } catch (error) {
    const isDefinitiveClientError =
      error instanceof GatewayRequestError &&
      error.status >= 400 &&
      error.status < 500 &&
      ![408, 429].includes(error.status)
    if (isDefinitiveClientError) {
      throw error
    }
    session = await createChatSession(machineId, idempotencyKey)
  }
  rememberSession(machineId, session.sessionId)
  return session
}

function validDate(value?: string) {
  if (!value) {
    return null
  }

  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function setPageInUrl(page: number | null) {
  const url = new URL(window.location.href)

  if (page) {
    url.searchParams.set('page', String(page))
  } else {
    url.searchParams.delete('page')
  }

  window.history.replaceState({}, '', url)
}

function formatDateTime(value?: string) {
  const date = validDate(value)
  if (!date) {
    return 'Unavailable'
  }

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

function formatDate(value?: string | null) {
  if (!value) {
    return 'Unavailable'
  }

  const date = new Date(`${value}T00:00:00`)
  if (Number.isNaN(date.getTime())) {
    return 'Unavailable'
  }

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
  }).format(date)
}

/**
 * Production rate against this machine's own rated speed.
 *
 * The bare number means little on its own: 4,500 bph is flat out for one
 * machine in this fleet and a tenth of another's capacity. The share of nominal
 * is what tells an operator whether what they are looking at is normal.
 */
function formatRate(telemetry?: TelemetrySnapshot | null) {
  if (typeof telemetry?.productionRateBph !== 'number') {
    return 'Unavailable'
  }

  const rate = `${telemetry.productionRateBph.toLocaleString()} bph`
  return typeof telemetry.rateUtilizationPct === 'number'
    ? `${rate} · ${Math.round(telemetry.rateUtilizationPct)}%`
    : rate
}

function formatPercent(value?: number | null) {
  return typeof value === 'number' ? `${Math.round(value)}%` : 'Unavailable'
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * The data domains this identity may read, named as an operator would.
 *
 * The server decides access; this only reports the decision it already sent, so
 * that a user who finds a panel locked can see why without guessing.
 */
function describeDomains(domains?: string[] | null) {
  if (!domains?.length) {
    return 'Unavailable'
  }

  const names: Record<string, string> = {
    common: 'machine details',
    operational: 'live status and maintenance',
    commercial: 'quotations and orders',
  }

  return domains.map((domain) => names[domain] ?? domain).join(', ')
}

/**
 * The manual's name with the part the header already said removed.
 *
 * Manifest titles read "M-EURO-VP-IES - use and maintenance manual, serial
 * 17478". The workspace header directly above reads "Serial 17478" over
 * "M-EURO-VP-IES", so on a phone that heading spent a line of a very small
 * screen repeating two things the operator had just read, and pushed the only
 * new word - which manual this is - off the end.
 *
 * A title that does not match the manifest's shape is left exactly as written;
 * trimming is only ever allowed to remove text this screen is already showing.
 */
function manualHeading(title: string | null | undefined, machine?: Machine | null) {
  if (!title) {
    return title ?? ''
  }

  let heading = title
  const model = machine?.model
  if (model) {
    // The separator is required, not optional. Without it a model name that
    // happens to open a sentence would be eaten out of the middle of a title
    // that was never in the manifest's "<model> - <document>" form.
    heading = heading.replace(
      new RegExp(`^${escapeRegExp(model)}\\s*[-–—:]\\s*`, 'i'),
      '',
    )
  }

  const serial = machine?.serialNumber
  if (serial) {
    heading = heading.replace(
      new RegExp(`[,;]?\\s*serial\\s+${escapeRegExp(serial)}\\s*$`, 'i'),
      '',
    )
  }

  heading = heading.trim()
  if (!heading) {
    return title
  }

  return heading.charAt(0).toUpperCase() + heading.slice(1)
}

/** Money in the currency the record was issued in, never converted. */
function formatMoney(value?: number | null, currency?: string | null) {
  if (value === null || value === undefined || !currency) {
    return 'Unavailable'
  }

  return new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency,
    maximumFractionDigits: 0,
  }).format(value)
}

function attachmentSize(bytes: number) {
  if (bytes < 1024) {
    return `${bytes} B`
  }

  if (bytes < 1024 * 1024) {
    return `${Math.ceil(bytes / 1024)} KiB`
  }

  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

function telemetryQuality(snapshot: TelemetrySnapshot | null) {
  if (!snapshot) {
    return { tone: 'error', label: 'Telemetry unavailable', detail: 'No recent readings are available.' }
  }

  const timestamp = validDate(snapshot.timestamp)
  const measuredAge =
    typeof snapshot.ageSeconds === 'number'
      ? snapshot.ageSeconds
      : timestamp
        ? Math.max(0, (Date.now() - timestamp.getTime()) / 1000)
        : null
  const isStale =
    snapshot.quality === 'stale' ||
    snapshot.quality === 'invalid' ||
    snapshot.quality === 'invalid-timestamp' ||
    !timestamp ||
    (measuredAge !== null &&
      typeof snapshot.staleAfterSeconds === 'number' &&
      measuredAge > snapshot.staleAfterSeconds)
  const isPartial = snapshot.quality === 'partial' || Boolean(snapshot.missingFields?.length)
  const tone = isStale ? 'error' : isPartial ? 'warning' : 'ready'
  const label = isStale ? 'Readings out of date' : isPartial ? 'Some readings missing' : 'Readings current'
  const age =
    measuredAge === null
      ? 'Update time unavailable'
      : measuredAge < 60
        ? `Updated ${Math.round(measuredAge)}s ago`
        : `Updated ${Math.round(measuredAge / 60)}m ago`
  const missing = snapshot.missingFields?.length
    ? ` Missing readings: ${snapshot.missingFields.join(', ')}.`
    : ''

  return {
    tone,
    label,
    detail: `${age}.${missing}`,
  }
}

function pageLink(item: Evidence) {
  if (!item.sourceUri) {
    return null
  }

  return getGatewayAssetUrl(`${item.sourceUri}${item.page ? `#page=${item.page}` : ''}`)
}

function manualIndexTone(index: ManualIndexStatus | null) {
  if (!index) {
    return 'warning'
  }

  if (index.status === 'unavailable' || index.status === 'empty' || index.totalIndexedChunks <= 0) {
    return 'error'
  }

  const incompleteManuals = index.manuals.filter(
    (manualEntry) =>
      !manualEntry.pdfExists ||
      manualEntry.status !== 'indexed' ||
      !manualEntry.indexedChunkCount ||
      manualEntry.indexedChunkCount <= 0,
  )

  return incompleteManuals.length > 0 ? 'warning' : 'ready'
}

function manualIndexLabel(index: ManualIndexStatus | null) {
  if (!index) {
    return 'Checking manual search'
  }

  if (index.status === 'unavailable') {
    return 'Manual search unavailable'
  }

  if (index.status === 'empty' || index.totalIndexedChunks <= 0) {
    return 'Manual search is not ready'
  }

  const incompleteManuals = index.manuals.filter(
    (manualEntry) =>
      !manualEntry.pdfExists ||
      manualEntry.status !== 'indexed' ||
      !manualEntry.indexedChunkCount ||
      manualEntry.indexedChunkCount <= 0,
  )

  if (incompleteManuals.length > 0) {
    return incompleteManuals.length === 1
      ? 'One manual is not searchable'
      : 'Some manuals are not searchable'
  }

  // Deliberately says nothing about chunk counts or the embedding model when
  // the index is healthy. Those are our plumbing, and they were the most
  // prominent line under the manual title on a phone. When something is wrong
  // the detail above stays, because then it is actionable.
  return 'Manual searchable'
}

interface AppProps {
  /**
   * Machine identifier or serial number, resolved by the router.
   *
   * Required, and deliberately not defaulted. This used to fall back to
   * reading the URL and then to a fixed machine, which meant a bad route
   * showed one machine's manual under another machine's address - a mistake
   * an operator cannot catch, because everything on the page looks consistent.
   */
  machineId: string
  session?: AuthSession | null
  onBackToFleet?: () => void
}

function App({ machineId, session: routedSession, onBackToFleet }: AppProps) {
  const initialManualPage = useMemo(() => getPageFromUrl(), [])
  const initialManualSection = useMemo(() => getSectionFromUrl(), [])
  // The phone layout is a different arrangement, not a narrower one, so it
  // branches rather than reflowing.
  const isMobile = useMediaQuery('(max-width: 899px)')
  const [machine, setMachine] = useState<Machine | null>(null)
  const [manual, setManual] = useState<Manual | null>(null)
  const [activeManualSourceUri, setActiveManualSourceUri] = useState<string | null>(null)
  const [manualPage, setManualPage] = useState<number | null>(initialManualPage)
  const [manualSection, setManualSection] = useState<string | null>(initialManualSection)
  const [manualIndex, setManualIndex] = useState<ManualIndexStatus | null>(null)
  const [manualIndexState, setManualIndexState] = useState<'loading' | 'live' | 'error'>('loading')
  const [telemetry, setTelemetry] = useState<TelemetrySnapshot | null>(null)
  const [contract, setContract] = useState<ServiceContract | null>(null)
  const [authSession, setAuthSession] = useState<AuthSession | null>(null)
  const [sessionId, setSessionId] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([initialAssistantMessage])
  const [messageEvidence, setMessageEvidence] = useState<MessageEvidence>({})
  const [selectedEvidenceMessageId, setSelectedEvidenceMessageId] = useState<string | null>(null)
  const chatListRef = useRef<HTMLDivElement | null>(null)
  const submissionInFlightRef = useRef(false)
  const [evidence, setEvidence] = useState<Evidence[]>([])
  const [isContextMenuOpen, setIsContextMenuOpen] = useState(false)
  const [isManualSheetOpen, setIsManualSheetOpen] = useState(false)
  const [isRailExpanded, setIsRailExpanded] = useState(false)
  const [railSection, setRailSection] = useState<ContextSection>('fleet')
  // The alarm code already handed to the assistant, so the banner's offer
  // does not sit there after it has been taken up.
  const [askedAlarm, setAskedAlarm] = useState<string | null>(null)
  const [isEvidenceOpen, setIsEvidenceOpen] = useState(false)
  const [diagnosticSteps, setDiagnosticSteps] = useState<DiagnosticStep[]>([])
  const [diagnosticChecks, setDiagnosticChecks] = useState<Record<string, boolean>>({})
  const [isDiagnosticOpen, setIsDiagnosticOpen] = useState(() => !isCompactViewport())
  const [draft, setDraft] = useState('')
  const [retryableSubmission, setRetryableSubmission] = useState<RetryableSubmission | null>(null)
  const [isSending, setIsSending] = useState(false)
  const [connectionState, setConnectionState] = useState<'loading' | 'live' | 'error'>('loading')
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loadAttempt, setLoadAttempt] = useState(0)
  /*
    The signed-in user's own details. The session carries the identifiers the
    access model runs on, not a name to show a person, so this resolves one from
    the demo directory. It is presentation only: nothing here is ever used to
    decide what may be read.
  */
  const [signedInUser, setSignedInUser] = useState<DemoUser | null>(null)
  const telemetryState = telemetryQuality(telemetry)
  const isChatBlocked = isSending || Boolean(retryableSubmission)

  const closeContextMenu = useCallback(() => setIsContextMenuOpen(false), [])
  const closeEvidence = useCallback(() => setIsEvidenceOpen(false), [])

  useEffect(() => {
    let ignore = false

    // Deliberately does not bootstrap a session. Root owns that and only does
    // it for a browser that arrives without one; calling it here logged in as
    // the configured default on every mount, which silently replaced a
    // signed-in technician with a full-access identity the moment they opened
    // a machine. Reading the session is enough.
    getAuthSession()
      .catch(() => null)
      .then(async (loadedAuthSession) => {
        if (loadedAuthSession?.mode === 'oidc' && !loadedAuthSession.authenticated) {
          startOidcLogin()
          return null
        }

        const [context, loadedSession, indexResult] = await Promise.all([
          getMachineContext(machineId),
          resumeOrCreateChatSession(machineId),
          getManualIndexStatus()
            .then((index) => ({ index, failed: false as const }))
            .catch(() => ({ index: null, failed: true as const })),
        ])
        return [context, loadedSession, indexResult, loadedAuthSession] as const
      })
      .then((result) => {
        if (!result) {
          return
        }

        const [context, loadedSession, indexResult, loadedAuthSession] = result
        if (ignore) {
          return
        }

        setMachine(context.machine)
        setManual(context.manual)
        setActiveManualSourceUri(null)
        setTelemetry(context.telemetry)
        setManualIndex(indexResult.index)
        setManualIndexState(indexResult.failed ? 'error' : 'live')
        setAuthSession(loadedAuthSession)
        setContract(context.contract)
        setSessionId(loadedSession.sessionId)
        rememberSession(machineId, loadedSession.sessionId)
        setMessages(loadedSession.messages.length > 0 ? loadedSession.messages : [initialAssistantMessage])
        setMessageEvidence(readStoredEvidence(loadedSession.sessionId))
        setSelectedEvidenceMessageId(null)
        setEvidence([])
        // Restored, not cleared: a reload mid-procedure used to drop the
        // checklist and every step already ticked.
        const storedDiagnostics = readStoredDiagnostics(loadedSession.sessionId)
        setDiagnosticSteps(storedDiagnostics.steps)
        setDiagnosticChecks(storedDiagnostics.checks)
        setRetryableSubmission(null)
        setConnectionState('live')

        if (initialManualSection && !initialManualPage) {
          void searchManualIndex(machineId, initialManualSection, 1)
            .then((result) => {
              if (ignore) {
                return
              }

              const sanitizedEvidence = sanitizeEvidence(result.evidence)
              const sectionEvidence =
                sanitizedEvidence.find((item) => item.source === 'manual') ?? sanitizedEvidence[0]
              if (!sectionEvidence) {
                return
              }

              setEvidence([sectionEvidence])
              if (sectionEvidence.sourceUri) {
                setActiveManualSourceUri(sectionEvidence.sourceUri)
              }
              if (sectionEvidence.page) {
                setManualPage(sectionEvidence.page)
                setPageInUrl(sectionEvidence.page)
              }
              setManualSection(sectionEvidence.section ?? sectionEvidence.title ?? initialManualSection)
            })
            .catch(() => undefined)
        }
      })
      .catch((error) => {
        if (ignore) {
          return
        }

        setLoadError(describeGatewayError(error))
        setConnectionState('error')
      })

    return () => {
      ignore = true
    }
  }, [initialManualPage, initialManualSection, loadAttempt, machineId])

  /*
    Put a name to the signed-in identifier. Only the demo directory can do this,
    so a failure or a real OIDC deployment simply leaves the identifier showing
    rather than blocking anything.
  */
  useEffect(() => {
    const userId = authSession?.userId
    let ignore = false

    const lookup = userId
      ? listDemoUsers().then(({ users }) => users.find((user) => user.userId === userId) ?? null)
      : Promise.resolve(null)

    lookup
      .then((user) => {
        if (!ignore) {
          setSignedInUser(user)
        }
      })
      .catch(() => {
        if (!ignore) {
          setSignedInUser(null)
        }
      })

    return () => {
      ignore = true
    }
  }, [authSession?.userId])

  useEffect(() => {
    if (sessionId) {
      rememberEvidence(sessionId, messageEvidence)
    }
  }, [messageEvidence, sessionId])

  useEffect(() => {
    if (sessionId) {
      rememberDiagnostics(sessionId, diagnosticSteps, diagnosticChecks)
    }
  }, [diagnosticSteps, diagnosticChecks, sessionId])

  useEffect(() => {
    const chatList = chatListRef.current
    if (!chatList) {
      return
    }

    chatList.scrollTo({
      top: chatList.scrollHeight,
      behavior: 'smooth',
    })
  }, [messages])

  function updateManualPage(page?: number | null) {
    if (!page) {
      return
    }

    setManualPage(page)
    setPageInUrl(page)
  }

  function retryWorkspace() {
    setConnectionState('loading')
    setLoadError(null)
    setManualIndexState('loading')
    setLoadAttempt((attempt) => attempt + 1)
  }

  function openManualPage(nextPage: number) {
    if (!Number.isInteger(nextPage) || nextPage < 1) {
      return
    }

    updateManualPage(nextPage)
  }

  function isCurrentEvidence(item: Evidence) {
    if (item.source !== 'manual') {
      return false
    }

    const itemUri = item.sourceUri ?? null
    const sameSource = itemUri === activeManualUri || (!itemUri && !activeManualUri)
    const samePage = item.page !== undefined && item.page === manualPage
    const sameSection =
      !samePage && manualSection !== null && (item.section === manualSection || item.title === manualSection)

    return sameSource && (samePage || sameSection)
  }

  function showCitation(item: Evidence) {
    if (item.sourceUri) {
      setActiveManualSourceUri(item.sourceUri)
    }

    if (item.source === 'manual') {
      setManualSection(item.section ?? item.title)
    }

    updateManualPage(item.page)
  }

  async function runSubmission(submission: RetryableSubmission, appendMessages: boolean) {
    if (!machine || !sessionId || submissionInFlightRef.current) {
      return
    }

    submissionInFlightRef.current = true

    if (appendMessages) {
      const summaries: ChatAttachmentSummary[] = submission.attachments.map(
        ({ id, name, type, size }) => ({ id, name, type, size }),
      )
      const userMessage: ChatMessage = {
        id: submission.userMessageId,
        role: 'user',
        content: submission.message,
        createdAt: new Date().toISOString(),
        attachments: summaries,
      }
      const assistantDraft: ChatMessage = {
        id: submission.assistantMessageId,
        role: 'assistant',
        content: '',
        createdAt: new Date().toISOString(),
      }
      setMessages((current) => [...current, userMessage, assistantDraft])
    } else {
      setMessages((current) =>
        current.map((message) =>
          message.id === submission.assistantMessageId ? { ...message, content: '' } : message,
        ),
      )
    }

    setRetryableSubmission(null)
    setIsSending(true)

    try {
      const response = await streamChatMessage(
        sessionId,
        machine.id,
        submission.message,
        submission.attachments,
        submission.idempotencyKey,
        (delta) => {
          setMessages((current) =>
            current.map((message) =>
              message.id === submission.assistantMessageId
                ? { ...message, content: `${message.content}${delta}` }
                : message,
            ),
          )
        },
      )

      setMessages((current) =>
        current.map((message) =>
          message.id === submission.assistantMessageId ? response.message : message,
        ),
      )
      const responseEvidence = sanitizeEvidence(response.evidence)
      setMessageEvidence((current) => ({
        ...current,
        [response.message.id]: responseEvidence,
      }))
      setSelectedEvidenceMessageId(response.message.id)
      setEvidence(responseEvidence)
      setDiagnosticSteps(response.diagnosticSteps ?? [])
      setDiagnosticChecks({})
      const firstManualEvidence = responseEvidence.find((item) => item.source === 'manual')
      if (firstManualEvidence) {
        showCitation(firstManualEvidence)
      }
      setConnectionState('live')
    } catch (error) {
      const isDefinitiveClientError =
        error instanceof GatewayRequestError &&
        error.status >= 400 &&
        error.status < 500 &&
        ![408, 429].includes(error.status)
      const errorMessage: ChatMessage = {
        id: submission.assistantMessageId,
        role: 'assistant',
        content: isDefinitiveClientError
          ? `The request was rejected with status ${error.status}. Check your access and message before sending again.`
          : 'The connection was interrupted before delivery could be confirmed. Try again.',
        createdAt: new Date().toISOString(),
      }

      setMessages((current) =>
        current.map((message) =>
          message.id === submission.assistantMessageId ? errorMessage : message,
        ),
      )
      setRetryableSubmission(isDefinitiveClientError ? null : submission)
    } finally {
      submissionInFlightRef.current = false
      setIsSending(false)
    }
  }

  async function submitMessage(nextMessage: string) {
    const trimmed = nextMessage.trim()

    if (!trimmed || isChatBlocked || !machine || !sessionId) {
      return
    }

    const submission: RetryableSubmission = {
      message: trimmed,
      attachments: [],
      idempotencyKey: createClientId(),
      userMessageId: createClientId(),
      assistantMessageId: createClientId(),
    }

    setDraft('')
    await runSubmission(submission, true)
  }

  async function retrySubmission() {
    if (!retryableSubmission || isSending) {
      return
    }

    await runSubmission(retryableSubmission, false)
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void submitMessage(draft)
  }

  async function signOut() {
    try {
      const logout = await logoutAuthSession()
      clearStoredChatState()
      if (logout.logoutRequired) {
        startProviderLogout()
        return
      }
      window.location.assign('/login')
    } catch {
      setLoadError('Sign out failed. Try again.')
    }
  }

  function diagnosticKey(step: DiagnosticStep, index: number) {
    return `${step.label}-${index}`
  }

  /** Only a failed step becomes a message; completion is tracked locally. */
  function diagnosticFollowUp(step: DiagnosticStep) {
    return step.failFollowUp ?? `I checked "${step.label}" and the issue is still present.`
  }

  function submitDiagnosticFollowUp(
    step: DiagnosticStep,
    index: number,
    outcome: 'completed' | 'failed',
  ) {
    /*
      Ticking a step off is bookkeeping, not a question.

      It used to send "I completed ..." as a chat message, which cost a full
      turn, replaced the whole step list with a freshly generated one, and
      scrolled the operator to the newest message - so working through a
      five-step checklist meant losing your place five times. Marking is local
      and instant now, and toggles, because a mis-tap on a plant floor should
      not need a round trip to undo.
    */
    if (outcome === 'completed') {
      const key = diagnosticKey(step, index)
      setDiagnosticChecks((current) => ({
        ...current,
        [key]: !current[key],
      }))
      return
    }

    // A step that failed is a real question, and does belong in the thread.
    void submitMessage(diagnosticFollowUp(step))
  }

  const manualSources = manual ? [manual, ...(manual.relatedManuals ?? [])] : []
  const activeManualUri = activeManualSourceUri ?? manual?.url ?? null
  const activeManual = manualSources.find((source) => source.url === activeManualUri)
  const isViewingRelatedManual = Boolean(manual && activeManualUri && activeManualUri !== manual.url)
  const manualUrl = activeManualUri
    ? getGatewayAssetUrl(`${activeManualUri}${manualPage ? `#page=${manualPage}` : ''}`)
    : null
  const manualIndexStatusLabel =
    manualIndexState === 'loading'
      ? 'Checking manual search'
      : manualIndexState === 'error'
        ? 'Manual search unavailable'
        : manualIndexLabel(manualIndex)
  const manualIndexStatusTone =
    manualIndexState === 'error' ? 'error' : manualIndexTone(manualIndex)
  const evidenceHeading = selectedEvidenceMessageId
    ? 'Message sources'
    : 'Sources'

  const renderContextPanel = (section: ContextSection, showHeading = true) => (
    <MachineContextPanel
      machineId={machineId}
      machine={machine}
      telemetry={telemetry}
      contract={contract}
      telemetryState={telemetryState}
      session={routedSession}
      section={section}
      manual={{
        title: manualHeading(activeManual?.title ?? manual?.title, machine),
        version: activeManual?.version ?? null,
        section: manualSection,
        page: manualPage,
        indexLabel: manualIndexStatusLabel,
        indexTone: manualIndexStatusTone,
        relatedSummary: manual?.relatedManuals?.length
          ? manual.relatedManuals.map((item) => item.title).join(', ')
          : null,
        isViewingRelatedManual,
      }}
      formatDateTime={formatDateTime}
      formatDate={formatDate}
      formatRate={formatRate}
      formatPercent={formatPercent}
      formatMoney={formatMoney}
      onBackToFleet={onBackToFleet}
      showHeading={showHeading}
    />
  )

  const evidenceDialog = (
    <EvidenceDialog
      open={isEvidenceOpen}
      heading={evidenceHeading}
      evidence={evidence}
      isCurrent={isCurrentEvidence}
      citationHref={pageLink}
      onShowCitation={(item) => {
        showCitation(item)
        closeEvidence()
        if (isMobile) {
          setIsManualSheetOpen(true)
        }
      }}
      onClose={closeEvidence}
    />
  )

  /*
    The phone layout. Chat first, the manual as a sheet over it, and the
    machine's full context behind the strip at the top.
  */
  if (isMobile) {
    if (connectionState === 'error') {
      return <Stack sx={{ p: 3, minHeight: '100dvh' }} spacing={2}>
        <Typography component="h1" variant="h3">Machine unavailable</Typography>
        <Alert severity="error">{loadError ?? 'Machine data could not be loaded.'}</Alert>
        <Button onClick={retryWorkspace}>Try again</Button>
        <Button component="a" href="/">Back to fleet</Button>
      </Stack>
    }
    return (
      <Box className="app-shell">
        <MobileWorkspace
          machine={machine}
          machineId={machineId}
          telemetry={telemetry}
          messages={messages}
          messageEvidence={messageEvidence}
          chatListRef={chatListRef}
          quickPrompts={quickPrompts}
          diagnosticSteps={diagnosticSteps}
          diagnosticChecks={diagnosticChecks}
          diagnosticKey={diagnosticKey}
          onDiagnosticFollowUp={submitDiagnosticFollowUp}
          draft={draft}
          isSending={isSending}
          isInitializing={connectionState === 'loading'}
          disabled={!machine || !sessionId || isChatBlocked}
          isContextOpen={isContextMenuOpen}
          retryableSubmission={Boolean(retryableSubmission)}
          onRetrySubmission={() => void retrySubmission()}
          onReloadWorkspace={retryWorkspace}
          attachmentSize={attachmentSize}
          formatRate={formatRate}
          onOpenContext={() => setIsContextMenuOpen(true)}
          onOpenManual={() => setIsManualSheetOpen(true)}
          onOpenEvidence={(messageId) => {
            setEvidence(messageEvidence[messageId])
            setSelectedEvidenceMessageId(messageId)
            setIsEvidenceOpen(true)
          }}
          onQuickPrompt={(prompt) => void submitMessage(prompt)}
          onDraftChange={setDraft}
          onSubmit={handleSubmit}
        />

        <Drawer
          variant="temporary"
          open={isContextMenuOpen}
          onClose={closeContextMenu}
          transitionDuration={MOTION.duration}
          slotProps={{
            transition: { easing: MOTION.easing },
            paper: {
              id: 'machine-context-panel',
              className: `machine-panel ${isContextMenuOpen ? 'open' : ''}`,
              role: 'dialog',
              'aria-label': 'Machine context',
              sx: {
                width: PANEL_WIDTH,
                maxWidth: '92vw',
                boxSizing: 'border-box',
                display: 'flex',
                flexDirection: 'column',
                overflow: 'hidden',
              },
            },
          }}
        >
          <Stack
            direction="row"
            sx={{
              flex: 'none',
              alignItems: 'center',
              justifyContent: 'space-between',
              px: 2,
              py: 1.25,
              borderBottom: '1px solid',
              borderColor: 'divider',
              bgcolor: M3.surfaceContainerLowest,
            }}
          >
            <Box>
              <Typography variant="overline" color="text.secondary" sx={{ display: 'block' }}>
                AROL Service Portal
              </Typography>
              <Typography variant="h3">Service Assist</Typography>
            </Box>
            <IconButton autoFocus onClick={closeContextMenu} aria-label="Close">
              <CloseIcon />
            </IconButton>
          </Stack>

          <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
            <Card
              className="identity-card"
              aria-label="Signed in"
              sx={{ mx: 2, mt: 2, mb: 1, p: 1.75 }}
            >
              <AccountDetails
                session={routedSession}
                signedInUser={signedInUser}
                contract={contract}
                describeDomains={describeDomains}
                onSignOut={() => void signOut()}
              />
            </Card>
            {/* No rail on a phone, so the whole context rides in one scroll. */}
            {renderContextPanel('all', false)}
          </Box>
        </Drawer>

        <ManualSheet
          open={isManualSheetOpen}
          onClose={() => setIsManualSheetOpen(false)}
          title={manualHeading(activeManual?.title ?? manual?.title, machine)}
          manualUrl={manualUrl}
          documentTitle={manual?.title ?? 'Machine manual'}
          page={manualPage}
          onStepPage={openManualPage}
          machineId={machine?.id ?? machineId}
          /*
            Only this machine's own manual. A related manual is a different
            document with its own front matter, and correcting its pages by this
            one's offset would be a new wrong number rather than a fix.
          */
          printedPageOffset={isViewingRelatedManual ? 0 : (manual?.printedPageOffset ?? 0)}
        />

        {evidenceDialog}
      </Box>
    )
  }

  /*
    The desktop layout. The rail is a 64px icon strip, the machine's identity
    lives in the header rather than in a card halfway down a scrolling column,
    and the account is an avatar menu in the top-right.
  */
  return (
    <Box className="app-shell" sx={{ display: 'flex', height: '100dvh', overflow: 'hidden' }}>
      <AppRail
        expanded={isRailExpanded}
        onToggle={() => setIsRailExpanded((expanded) => !expanded)}
        active={railSection === 'tickets' || railSection === 'quotes' ? railSection : 'fleet'}
        onSelect={(target) => {
          // Tapping the section already open closes it, so each tab is its own
          // toggle and the rail needs no second control for that.
          if (isRailExpanded && railSection === target) {
            setIsRailExpanded(false)
            return
          }

          setRailSection(target)
          setIsRailExpanded(true)
        }}
      />

      {/* Expanding the rail restores the full machine panel. */}
      <Drawer
        id="machine-context-panel"
        className={`machine-panel ${isRailExpanded ? 'open' : ''}`}
        variant="persistent"
        open={isRailExpanded}
        transitionDuration={MOTION.duration}
        slotProps={{
          transition: { easing: MOTION.easing },
          paper: {
            'aria-label': 'Machine context',
            sx: {
              width: PANEL_WIDTH,
              position: 'relative',
              boxSizing: 'border-box',
              overflowX: 'hidden',
            },
          },
        }}
        sx={{
          width: isRailExpanded ? PANEL_WIDTH : 0,
          flexShrink: 0,
          overflow: 'hidden',
          willChange: 'width',
          transition: `width ${isRailExpanded ? MOTION.duration.enter : MOTION.duration.exit}ms ${
            isRailExpanded ? MOTION.easing.enter : MOTION.easing.exit
          }`,
        }}
      >
        {renderContextPanel(railSection)}
      </Drawer>

      <Box
        component="section"
        className="workspace"
        sx={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, overflow: 'hidden' }}
      >
        <AppBar position="static" elevation={0} className="workspace-header">
          <Toolbar
            sx={{
              gap: 1.75,
              minHeight: 60,
              bgcolor: M3.surfaceContainerLowest,
              borderBottom: '1px solid',
              borderColor: M3.surfaceContainerHighest,
            }}
          >
            <Typography variant="h3" noWrap className="workspace-title" sx={{ minWidth: 0 }}>
              {machine?.model ?? machine?.id ?? machineId}
            </Typography>

            {machine?.serialNumber ? (
              <Chip size="small" label={`SERIAL ${machine.serialNumber}`} />
            ) : null}

            <Chip
              size="small"
              className="status-pill"
              color={statusSeverity(machine?.status)}
              label={displayLabel(machine?.status, 'Loading')}
            />

            {machine?.plant ? (
              <Chip
                size="small"
                variant="outlined"
                label={machine.plant}
                sx={{ fontFamily: 'inherit' }}
              />
            ) : null}

            <Box sx={{ flex: 1 }} />

            <Chip
              size="small"
              label={formatRate(telemetry)}
              sx={{ bgcolor: M3.surfaceContainerLow, fontFamily: 'inherit' }}
            />

            <AccountMenu
              session={routedSession}
              signedInUser={signedInUser}
              contract={contract}
              describeDomains={describeDomains}
              onSignOut={() => void signOut()}
            />
          </Toolbar>
        </AppBar>

        {/*
          Condition without opening anything. Scanning the code on a machine is
          a question about how it is running, and the answer used to be behind a
          drawer.
        */}
        <Box component="section" aria-label="Machine condition" sx={{ px: 2.5, pt: 1.75 }}>
          {telemetry?.activeAlarm ? (
            <Stack
              direction="row"
              spacing={1.5}
              className="condition-alarm"
              role="status"
              sx={{
                alignItems: 'center',
                bgcolor: M3.errorContainer,
                borderLeft: `4px solid ${M3.error}`,
                borderRadius: 2,
                px: 1.75,
                py: 1.25,
              }}
            >
              <Box
                sx={{
                  width: 16,
                  height: 16,
                  borderRadius: 999,
                  border: `2px solid ${M3.onErrorContainer}`,
                  flex: 'none',
                }}
              />
              <Typography
                sx={{
                  fontFamily: MONO,
                  fontSize: '0.75rem',
                  fontWeight: 500,
                  color: M3.onErrorContainer,
                }}
              >
                {telemetry.activeAlarm}
              </Typography>
              <Box sx={{ flex: 1 }} />
              {askedAlarm === telemetry.activeAlarm ? null : (
                <Button
                  size="small"
                  disabled={!machine || !sessionId || isChatBlocked}
                  onClick={() => {
                    setAskedAlarm(telemetry.activeAlarm)
                    void submitMessage(`Walk me through clearing ${telemetry.activeAlarm}.`)
                  }}
                  sx={{ bgcolor: M3.onErrorContainer, '&:hover': { bgcolor: M3.error } }}
                >
                  Ask Service Assist
                </Button>
              )}
            </Stack>
          ) : (
            <Alert severity="success" sx={{ py: 0.75 }}>
              <Typography sx={{ fontFamily: MONO, fontSize: '0.8125rem' }}>
                No active alarm · {formatRate(telemetry)}
              </Typography>
            </Alert>
          )}
        </Box>

        {connectionState === 'error' ? (
          <Alert
            severity="error"
            className="workspace-alert"
            sx={{ mx: 2.5, mt: 1.5 }}
            action={
              <Button size="small" onClick={retryWorkspace}>
                Retry
              </Button>
            }
          >
            {loadError ?? 'The gateway or AI service is unavailable.'}
          </Alert>
        ) : null}

        <Box
          className="work-grid"
          sx={{ flex: 1, minHeight: 0, display: 'flex', gap: 2, px: 2.5, pt: 1.75, pb: 2.5 }}
        >
          <Box sx={{ flex: 1, minWidth: 0, display: 'flex' }}>
            <ManualPane
              manualUrl={manualUrl}
              iframeTitle={manual?.title ?? 'Machine manual'}
            />
          </Box>

          <Box sx={{ width: 420, flex: 'none', display: 'flex' }}>
            <ChatPane
              messages={messages}
              messageEvidence={messageEvidence}
              chatListRef={chatListRef}
              evidence={evidence}
              onOpenEvidence={(messageId) => {
                if (messageId) {
                  setEvidence(messageEvidence[messageId])
                }
                setSelectedEvidenceMessageId(messageId)
                setIsEvidenceOpen(true)
              }}
              diagnosticSteps={diagnosticSteps}
              diagnosticChecks={diagnosticChecks}
              diagnosticKey={diagnosticKey}
              isDiagnosticOpen={isDiagnosticOpen}
              onDiagnosticOpenChange={setIsDiagnosticOpen}
              onDiagnosticFollowUp={submitDiagnosticFollowUp}
              retryableSubmission={Boolean(retryableSubmission)}
              onRetrySubmission={() => void retrySubmission()}
              onReloadWorkspace={retryWorkspace}
              quickPrompts={quickPrompts}
              onQuickPrompt={(prompt) => void submitMessage(prompt)}
              attachmentSize={attachmentSize}
              draft={draft}
              onDraftChange={setDraft}
              onSubmit={handleSubmit}
              isSending={isSending}
              isInitializing={connectionState === 'loading'}
              disabled={!machine || !sessionId || isChatBlocked}
            />
          </Box>
        </Box>
      </Box>

      {evidenceDialog}
    </Box>
  )
}

export default App
