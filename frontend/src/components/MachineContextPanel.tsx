import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Card from '@mui/material/Card'
import CardContent from '@mui/material/CardContent'
import Chip from '@mui/material/Chip'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'

import { CommercialPanel, LockedPanel, MaintenancePanel } from './MachineDataPanels'
import { MONO, displayLabel, statusSeverity } from '../theme'
import type {
  AuthSession,
  Machine,
  ServiceContract,
  TelemetrySnapshot,
} from '../types/contracts'

interface TelemetryState {
  tone: string
  label: string
  detail: string
}

/** What the DOC section knows about the manual on screen. */
export interface ManualSummary {
  title: string
  version: string | null
  section: string | null
  page: number | null
  indexLabel: string
  indexTone: 'ready' | 'warning' | 'error'
  relatedSummary: string | null
  isViewingRelatedManual: boolean
}

/**
 * Which section the panel is showing.
 *
 * The desktop rail picks one; the phone drawer, which has no rail, shows the
 * lot in one scroll.
 */
export type ContextSection = 'all' | 'fleet' | 'doc' | 'tickets' | 'quotes'

interface MachineContextPanelProps {
  machineId: string
  machine: Machine | null
  telemetry: TelemetrySnapshot | null
  contract: ServiceContract | null
  telemetryState: TelemetryState
  session: AuthSession | null | undefined
  section?: ContextSection
  manual?: ManualSummary | null
  formatDateTime: (value?: string) => string
  formatDate: (value?: string | null) => string
  formatRate: (telemetry?: TelemetrySnapshot | null) => string
  formatPercent: (value?: number | null) => string
  formatMoney: (value?: number | null, currency?: string | null) => string
  onBackToFleet?: () => void
  showHeading?: boolean
}

const TONE_COLOR = {
  ready: 'success.main',
  warning: 'warning.main',
  error: 'error.main',
} as const

/** Material's severity for the telemetry-quality banner. */
function telemetrySeverity(tone: string) {
  if (tone === 'ok' || tone === 'ready') return 'success' as const
  if (tone === 'warn' || tone === 'warning') return 'warning' as const
  return 'error' as const
}

/** One reading in the telemetry strip. */
function Reading({ label, value }: { label: string; value: string }) {
  return (
    <Box>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
        {label}
      </Typography>
      <Typography
        sx={{ fontFamily: MONO, fontSize: '1.0625rem', fontWeight: 500, lineHeight: 1.4 }}
      >
        {value}
      </Typography>
    </Box>
  )
}

/** A label/value row in one of the detail cards. */
function Detail({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <Stack direction="row" spacing={1} sx={{ justifyContent: 'space-between' }}>
      <Typography variant="body2" color="text.secondary">
        {label}
      </Typography>
      <Typography
        variant="body2"
        sx={{ textAlign: 'right', overflowWrap: 'anywhere', fontFamily: mono ? MONO : 'inherit' }}
      >
        {value}
      </Typography>
    </Stack>
  )
}

/**
 * Everything known about the machine in front of the operator.
 *
 * Each domain is either shown or explicitly locked. A panel the signed-in role
 * may not read is never blanked out, because "Unavailable" and "you may not see
 * this" are different statements and the dataset brief insists on the second.
 */
export function MachineContextPanel({
  machineId,
  machine,
  telemetry,
  contract,
  telemetryState,
  session,
  section = 'all',
  manual,
  formatDateTime,
  formatDate,
  formatRate,
  formatPercent,
  formatMoney,
  onBackToFleet,
  showHeading = true,
}: MachineContextPanelProps) {
  const seesCommercial = session?.domains?.includes('commercial') ?? false
  const seesOperational = session?.domains?.includes('operational') ?? false

  const showAll = section === 'all'
  const showFleet = showAll || section === 'fleet'
  const showDoc = showAll || section === 'doc'
  const showQuotes = showAll || section === 'quotes'
  const showTickets = showAll || section === 'tickets'

  return (
    <Stack spacing={2} sx={{ p: 2 }}>
      {showHeading ? (
        <Box>
          <Typography variant="overline" color="text.secondary" sx={{ display: 'block' }}>
            AROL Service Portal
          </Typography>
          <Typography variant="h2" component="h1">
            Service Assist
          </Typography>
        </Box>
      ) : null}

      {showFleet && onBackToFleet ? (
        <Button variant="outlined" onClick={onBackToFleet} sx={{ alignSelf: 'flex-start' }}>
          All machines
        </Button>
      ) : null}

      {showFleet ? (
        <>
          <Card>
            <CardContent>
              <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mb: 1 }}>
                <Typography variant="h3" sx={{ flexGrow: 1 }}>
                  {machine?.model ?? 'Loading machine'}
                </Typography>
                <Chip
                  size="small"
                  color={statusSeverity(machine?.status)}
                  label={displayLabel(machine?.status, 'Loading')}
                />
              </Stack>
              <Stack spacing={0.5}>
                <Detail label="Machine ID" value={machine?.id ?? machineId} mono />
                <Detail label="Serial" value={machine?.serialNumber ?? 'Unavailable'} mono />
                <Detail label="Plant" value={machine?.plant ?? 'Unavailable'} />
                <Detail
                  label="Last telemetry"
                  value={formatDateTime(telemetry?.timestamp ?? machine?.lastTelemetryAt)}
                />
              </Stack>
            </CardContent>
          </Card>

          {/*
            The readings this dataset actually records. It has no RPM and no
            torque, so those tiles said "Unavailable" on every machine forever
            while the real figures were shown nowhere.
          */}
          <Card component="section" aria-label="Telemetry snapshot" sx={{ p: 2 }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: 'baseline', mb: 1.5 }}>
              <Typography variant="subtitle2" sx={{ flexGrow: 1 }}>
                Live readings
              </Typography>
              {/*
                Whether the numbers below are current. A stale reading that looks
                live is worse than no reading, so the quality travels with them
                rather than sitting in a banner somewhere else.
              */}
              <Typography
                variant="caption"
                role="status"
                title={telemetryState.detail}
                color={`${telemetrySeverity(telemetryState.tone)}.main`}
              >
                {telemetryState.label}
              </Typography>
            </Stack>
            <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(2, 1fr)' }}>
              <Reading label="Rate" value={formatRate(telemetry)} />
              <Reading label="Uptime" value={formatPercent(telemetry?.uptimePercentage)} />
              <Reading
                label="Temp"
                value={
                  typeof telemetry?.temperatureC === 'number'
                    ? `${telemetry.temperatureC} °C`
                    : 'Unavailable'
                }
              />
              <Reading
                label="New alarms (1h)"
                value={
                  typeof telemetry?.alarmCount === 'number'
                    ? String(telemetry.alarmCount)
                    : 'Unavailable'
                }
              />
            </Box>
          </Card>

          {/*
            The alarm gets a full-width row of its own: an alarm code is long, it
            is the most important thing on the screen, and in a tile it was
            truncated to "AL019_CAPS_SORTI".
          */}
          <Alert
            severity={telemetry?.activeAlarm ? 'error' : 'success'}
            variant="outlined"
            aria-label="Active alarm"
          >
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
              Active alarm
            </Typography>
            <Typography variant="subtitle2" sx={{ overflowWrap: 'anywhere' }}>
              {telemetry?.activeAlarm ?? 'None'}
            </Typography>
          </Alert>

          {/*
            No warranty or SLA row: the fleet dataset records neither, and a
            field reading "Unavailable" beside the word Warranty is read as "not
            covered". Delivery and acquisition value are commercial data, so a
            user whose visibility excludes that domain gets the locked panel.
          */}
          {seesCommercial ? (
            <Card>
              <CardContent>
                <Typography variant="h3" gutterBottom>
                  Service record
                </Typography>
                <Stack spacing={0.5}>
                  <Detail label="Delivered" value={formatDate(contract?.deliveryDate)} />
                  <Detail
                    label="Acquisition value"
                    value={formatMoney(contract?.acquisitionCost, contract?.currency)}
                  />
                  {seesOperational ? <Detail
                    label="Open tickets"
                    value={
                      contract
                        ? `${contract.openTicketCount ?? 0} of ${contract.ticketCount ?? 0}`
                        : 'Unavailable'
                    }
                  /> : null}
                </Stack>
                {contract?.coverageNote ? (
                  <Typography
                    variant="caption"
                    color="text.secondary"
                    sx={{ display: 'block', mt: 1 }}
                  >
                    Contact AROL service for warranty or SLA information.
                  </Typography>
                ) : null}
              </CardContent>
            </Card>
          ) : (
            <LockedPanel
              what="delivery, acquisition value and quotations"
              visibility={session?.visibility}
            />
          )}
        </>
      ) : null}

      {/*
        The document tab. The manual itself fills the pane beside this, so what
        belongs here is what the pane cannot show: which manual this is, whether
        it is actually searchable, and what else was indexed alongside it.
      */}
      {showDoc && !showAll ? (
        <Card>
          <CardContent>
            <Typography variant="h3" gutterBottom>
              Manual
            </Typography>
            <Stack spacing={0.5}>
              <Detail label="Title" value={manual?.title || 'No manual loaded'} />
              <Detail label="Version" value={manual?.version ? `v${manual.version}` : 'Unavailable'} />
              <Detail label="Page" value={manual?.page ? String(manual.page) : 'Not set'} />
              {manual?.section ? <Detail label="Cited section" value={manual.section} /> : null}
            </Stack>

            <Typography
              variant="body2"
              role="status"
              sx={{ mt: 1.5, color: TONE_COLOR[manual?.indexTone ?? 'warning'] }}
            >
              {manual?.indexLabel ?? 'Index status unavailable'}
            </Typography>

            {manual?.isViewingRelatedManual ? (
              <Chip size="small" label="Related source" sx={{ mt: 1 }} />
            ) : null}

            {manual?.relatedSummary ? (
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
                Related source: {manual.relatedSummary}
              </Typography>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {showQuotes ? (
        seesCommercial ? (
          <CommercialPanel machineId={machineId} />
        ) : (
          <LockedPanel what="quotations, orders and pricing" visibility={session?.visibility} />
        )
      ) : null}

      {showTickets ? (
        seesOperational ? (
          <MaintenancePanel machineId={machineId} />
        ) : (
          <LockedPanel
            what="telemetry, alarms and maintenance history"
            visibility={session?.visibility}
          />
        )
      ) : null}
    </Stack>
  )
}
