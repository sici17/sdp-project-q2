import { useEffect, useState } from 'react'
import Alert from '@mui/material/Alert'
import AppBar from '@mui/material/AppBar'
import Box from '@mui/material/Box'
import Card from '@mui/material/Card'
import CardActionArea from '@mui/material/CardActionArea'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import Container from '@mui/material/Container'
import Stack from '@mui/material/Stack'
import Toolbar from '@mui/material/Toolbar'
import Typography from '@mui/material/Typography'

import { describeGatewayError, getFleet, listDemoUsers } from '../services/api'
import { statusSeverity } from '../theme'
import type { AuthSession, DemoUser, MachineSummary } from '../types/contracts'
import { AccountMenu } from './AccountMenu'

const STATUS_LABEL: Record<string, string> = {
  ok: 'Running',
  warning: 'Attention',
  critical: 'Alarm',
  idle: 'Idle',
  unknown: 'Not shown',
}

interface FleetViewProps {
  session: AuthSession | null
  onOpenMachine: (machineId: string) => void
  onSignOut: () => void
}

/**
 * The machine park of the signed-in user's company.
 *
 * Scoped by the gateway, not filtered here: another company's machines are
 * never sent. A company that owns no machines gets an honest empty state, which
 * is a true statement about their own fleet rather than a refusal.
 */
export function FleetView({ session, onOpenMachine, onSignOut }: FleetViewProps) {
  const [machines, setMachines] = useState<MachineSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  /*
    The signed-in user's own details, resolved the same way the machine screen
    resolves them. The session carries the identifiers the access model runs on,
    not a name or a company to show a person. Presentation only: nothing here
    decides what may be read.
  */
  const [signedInUser, setSignedInUser] = useState<DemoUser | null>(null)

  useEffect(() => {
    let cancelled = false
    getFleet()
      .then((payload) => {
        if (!cancelled) {
          setMachines(payload.machines)
        }
      })
      .catch((cause) => {
        if (!cancelled) {
          setError(describeGatewayError(cause))
          setMachines([])
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const userId = session?.userId
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
          // A failed directory lookup costs a display name, not access. The
          // menu falls back to the session's own subject.
          setSignedInUser(null)
        }
      })

    return () => {
      ignore = true
    }
  }, [session?.userId])

  const seesOperational = session?.domains?.includes('operational') ?? false

  return (
    <Box>
      <AppBar component="header" position="static" color="default" elevation={0} variant="outlined">
        <Toolbar
          sx={{
            gap: { xs: 1.5, sm: 2 },
            py: { xs: 1.5, sm: 0 },
            // One row at every width. The title used to claim the full basis on
            // a phone, which wrapped the account avatar onto a line of its own
            // and left a band of empty header above the machine list.
            alignItems: 'center',
            flexWrap: 'nowrap',
          }}
        >
          <Box sx={{ flexGrow: 1, minWidth: 0 }}>
            {/* <Typography
              variant="overline"
              color="text.secondary"
              sx={{ display: 'block', lineHeight: 1.2 }}
            >
              Equipment
            </Typography> */}
            <Typography variant="h2" component="h1">
              Your machines
            </Typography>
            <Typography variant="body2" color="text.secondary" noWrap>
              {signedInUser?.companyName ?? session?.companyId ?? 'Your company'}
            </Typography>
          </Box>
          {/*
            The same account menu the machine screen uses, so the identity reads
            identically on both screens. `contract` is machine-scoped and there
            is no machine here, so the maintenance row is simply absent rather
            than being filled with a number that would mean something else.
          */}
          <Box sx={{ flex: 'none', ml: 'auto' }}>
            <AccountMenu
              session={session}
              signedInUser={signedInUser}
              contract={null}
              onSignOut={onSignOut}
            />
          </Box>
        </Toolbar>
      </AppBar>

      <Container component="main" maxWidth="lg" sx={{ py: 3 }}>
        {error ? (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        ) : null}

        {machines === null ? (
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
            <CircularProgress size={20} />
            <Typography color="text.secondary">Loading your machines…</Typography>
          </Stack>
        ) : null}

        {machines !== null && machines.length === 0 && !error ? (
          <Alert severity="info" role="status">
            No machines are registered to your company yet. Manuals and machine data will appear
            here once a machine is installed.
          </Alert>
        ) : null}

        <Box
          sx={{
            display: 'grid',
            gap: 2,
            gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, 1fr)', lg: 'repeat(3, 1fr)' },
          }}
        >
          {(machines ?? []).map((machine) => (
            <Card key={machine.id} className="machine-card">
              <CardActionArea
                onClick={() => onOpenMachine(machine.id)}
                sx={{ p: 2, height: '100%', alignItems: 'flex-start' }}
              >
                <Stack spacing={0.5} sx={{ alignItems: 'flex-start' }}>
                  <Typography variant="h3">{machine.model}</Typography>
                  <Typography variant="body2" color="text.secondary">
                    {machine.id} · serial {machine.serialNumber}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {machine.plant}
                  </Typography>
                  {machine.nominalRateBph ? (
                    <Typography variant="caption" color="text.secondary">
                      {machine.nominalRateBph.toLocaleString()} bph nominal
                      {machine.headsCount ? ` · ${machine.headsCount} heads` : ''}
                    </Typography>
                  ) : null}
                  {/* Live health is operational data, so it is shown only to the
                      roles that may read it rather than being blanked out silently. */}
                  {seesOperational ? (
                    <Chip
                      size="small"
                      color={statusSeverity(machine.status)}
                      label={STATUS_LABEL[machine.status] ?? machine.status}
                    />
                  ) : (
                    <Chip size="small" variant="outlined" label="Status restricted" />
                  )}
                </Stack>
              </CardActionArea>
            </Card>
          ))}
        </Box>
      </Container>
    </Box>
  )
}
