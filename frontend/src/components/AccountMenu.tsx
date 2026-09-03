import { UserAvatar } from './Identity'
import { useState, type MouseEvent } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Divider from '@mui/material/Divider'
import IconButton from '@mui/material/IconButton'
import Popover from '@mui/material/Popover'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'

import { M3 } from '../theme'
import type { AuthSession, DemoUser, ServiceContract } from '../types/contracts'

interface AccountMenuProps {
  session: AuthSession | null | undefined
  signedInUser: DemoUser | null
  contract: ServiceContract | null
  /**
   * Optional: the fleet screen has no machine, so it has no domain summary to
   * hand over. `AccountDetails` does not read it.
   */
  describeDomains?: (domains?: string[] | null) => string
  onSignOut: () => void
}

/** One line in the menu body. */
function Row({ children, muted }: { children: string; muted?: boolean }) {
  return (
    <Typography
      variant="body2"
      sx={{
        // The design's 32px line-height keeps the rhythm of single-line rows,
        // but a wrapped row - the access list, in a narrow drawer card - turns
        // into a widely spaced paragraph. Keep the height, not the leading.
        display: 'flex',
        alignItems: 'center',
        minHeight: 32,
        lineHeight: 1.5,
        color: muted ? M3.onSurfaceVariant : M3.onSurface,
      }}
    >
      {children}
    </Typography>
  )
}

/**
 * Who is signed in, as an avatar menu in the top-right.
 *
 * This used to be a card in the middle of the scrolling context panel, which
 * put an account action - sign out - among the machine's data, where it read as
 * something you did to the machine.
 */
export function AccountMenu({
  session,
  signedInUser,
  contract,
  describeDomains,
  onSignOut,
}: AccountMenuProps) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const name = signedInUser?.name ?? session?.subject ?? 'Unavailable'

  const open = (event: MouseEvent<HTMLElement>) => setAnchor(event.currentTarget)
  const close = () => setAnchor(null)

  return (
    <>
      <IconButton
        onClick={open}
        aria-label="Account"
        aria-haspopup="true"
        aria-expanded={Boolean(anchor)}
        sx={{ p: 0.5 }}
      >
        <UserAvatar name={name} size={36} />
      </IconButton>

      <Popover
        open={Boolean(anchor)}
        anchorEl={anchor}
        onClose={close}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
        slotProps={{
          paper: {
            className: 'identity-card',
            'aria-label': 'Signed in',
            sx: {
              width: 260,
              p: 1.75,
              mt: 1,
              borderRadius: 3,
              border: `1px solid ${M3.outlineVariant}`,
              boxShadow: '0 8px 24px rgba(25, 28, 30, .18)',
            },
          },
        }}
      >
        <AccountDetails
          session={session}
          signedInUser={signedInUser}
          contract={contract}
          describeDomains={describeDomains}
          onSignOut={onSignOut}
        />
      </Popover>
    </>
  )
}

/**
 * The signed-in identity, gateway status and the way out.
 *
 * Rendered inside the desktop avatar menu and, on a phone - which has no top
 * bar to hang an avatar from - as a card at the top of the machine drawer.
 */
export function AccountDetails({
  session,
  signedInUser,
  contract,
  describeDomains,
  onSignOut,
}: AccountMenuProps) {
  const name = signedInUser?.name ?? session?.subject ?? 'Unavailable'
  const company = signedInUser?.companyName ?? session?.companyId ?? 'Unavailable'

  return (
    <>
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
        <UserAvatar name={name} size={40} />
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="body2" sx={{ fontWeight: 500 }} noWrap>
            {name}
          </Typography>
          {signedInUser?.jobTitle ? (
            <Typography variant="caption" sx={{ color: M3.outline }} noWrap>
              {signedInUser.jobTitle}
            </Typography>
          ) : null}
        </Box>
      </Stack>

      <Divider sx={{ my: 1.5 }} />

      <Stack spacing={0.25}>
        <Row>{company}</Row>
        {/*
          What this identity may read. The product's whole argument is that two
          colleagues at the same company get different answers, so the account
          card is where a person checks which of the two they are.
        */}
        {session?.domains?.length ? (
          <Row muted>
            {describeDomains ? describeDomains(session.domains) : session.domains.join(', ')}
          </Row>
        ) : null}
        {contract && session?.domains?.includes('operational') ? (
          <Row muted>
            {`Maintenance · ${contract.openTicketCount ?? 0} of ${contract.ticketCount ?? 0} tickets`}
          </Row>
        ) : null}
      </Stack>

      {session?.authenticated ? (
        <>
          <Divider sx={{ my: 1.5 }} />
          <Stack direction="row" sx={{ justifyContent: 'flex-end' }}>
            <Button variant="text" size="small" onClick={onSignOut} sx={{ minHeight: 44, px: 1 }}>
              Sign out
            </Button>
          </Stack>
        </>
      ) : null}
    </>
  )
}
