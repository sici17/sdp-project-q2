import { useEffect, useMemo, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Card from '@mui/material/Card'
import CardActionArea from '@mui/material/CardActionArea'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import Container from '@mui/material/Container'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'

import { displayLabel } from '../theme'

import { describeGatewayError, listDemoUsers, signInAs } from '../services/api'
import type { DemoUser } from '../types/contracts'

const VISIBILITY_HELP: Record<string, string> = {
  full: 'Machines, manuals, telemetry and commercial records',
  technician: 'Machines, manuals, telemetry, alarms and maintenance',
  commercial: 'Machines, manuals, quotations and orders',
}

/** Material's colour roles for the three visibility levels in the dataset. */
const VISIBILITY_COLOR: Record<string, 'success' | 'info' | 'warning' | 'default'> = {
  full: 'success',
  technician: 'info',
  commercial: 'warning',
}

interface SignInScreenProps {
  onSignedIn: () => void
}

/**
 * Sign in as one of the fleet dataset's users.
 *
 * The demo has no passwords: the dataset states every account is active, and
 * what this project needs to show is authorization rather than authentication.
 * Each identity carries a company and a visibility level, and everything the
 * signed-in user can reach follows from those two.
 */
export function SignInScreen({ onSignedIn }: SignInScreenProps) {
  const [users, setUsers] = useState<DemoUser[]>([])
  const [error, setError] = useState<string | null>(null)
  const [pendingUserId, setPendingUserId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    listDemoUsers()
      .then((payload) => {
        if (!cancelled) {
          setUsers(payload.users)
          setLoading(false)
        }
      })
      .catch((cause) => {
        if (!cancelled) {
          setError(describeGatewayError(cause))
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const byCompany = useMemo(() => {
    const groups = new Map<string, { companyName: string; country: string; users: DemoUser[] }>()
    for (const user of users) {
      const group = groups.get(user.companyId) ?? {
        companyName: user.companyName,
        country: user.country,
        users: [],
      }
      group.users.push(user)
      groups.set(user.companyId, group)
    }
    return [...groups.entries()]
  }, [users])

  async function handleSignIn(user: DemoUser) {
    setPendingUserId(user.userId)
    setError(null)
    try {
      await signInAs(user.userId)
      onSignedIn()
    } catch (cause) {
      setError(describeGatewayError(cause))
      setPendingUserId(null)
    }
  }

  return (
    <Container component="main" maxWidth="lg" sx={{ py: 4 }}>
      <Box component="header" sx={{ mb: 3 }}>
        <Typography variant="overline" color="text.secondary">
          AROL Service Portal
        </Typography>
        <Typography variant="h1" gutterBottom>
          Choose an account
        </Typography>
        {/* <Typography color="text.secondary">Access is based on company and role.</Typography> */}
      </Box>

      {error ? (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      ) : null}

      {loading ? (
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mb: 2 }}>
          <CircularProgress size={20} />
          <Typography color="text.secondary">Loading identities…</Typography>
        </Stack>
      ) : null}

      <Stack spacing={4}>
        {byCompany.map(([companyId, group]) => (
          <Box key={companyId} component="section" aria-label={group.companyName}>
            <Typography variant="h2" gutterBottom>
              {group.companyName}{' '}
              <Typography component="span" color="text.secondary" variant="body2">
                {group.country}
              </Typography>
            </Typography>
            <Box
              sx={{
                display: 'grid',
                gap: 2,
                gridTemplateColumns: {
                  xs: '1fr',
                  sm: 'repeat(2, 1fr)',
                  md: 'repeat(4, 1fr)',
                },
              }}
            >
              {group.users.map((user) => (
                <Card key={user.userId} className="identity-card">
                  <CardActionArea
                    onClick={() => void handleSignIn(user)}
                    disabled={pendingUserId !== null}
                    sx={{ p: 2, height: '100%', alignItems: 'flex-start' }}
                  >
                    <Stack spacing={0.5} sx={{ alignItems: 'flex-start' }}>
                      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
                        {user.name}
                      </Typography>
                      <Typography variant="body2" color="text.secondary">
                        {user.jobTitle}
                      </Typography>
                      <Chip
                        size="small"
                        label={displayLabel(user.visibility)}
                        color={VISIBILITY_COLOR[user.visibility] ?? 'default'}
                      />
                      <Typography variant="caption" color="text.secondary">
                        {VISIBILITY_HELP[user.visibility] ?? user.domains.join(', ')}
                      </Typography>
                      {pendingUserId === user.userId ? (
                        <Typography variant="caption" color="primary">
                          Signing in…
                        </Typography>
                      ) : null}
                    </Stack>
                  </CardActionArea>
                </Card>
              ))}
            </Box>
          </Box>
        ))}
      </Stack>
    </Container>
  )
}
