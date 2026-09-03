import Avatar from '@mui/material/Avatar'
import Box from '@mui/material/Box'

import { M3, MONO, displayLabel, statusPillColors } from '../theme'

/**
 * The small shared marks that appear on both layouts.
 *
 * A status pill and an avatar each existed twice - once as a MUI component on
 * the desktop screen and once hand-built in the phone workspace, in one case
 * under the same `status-pill` class name. That is not only duplication: when
 * the chip colours were corrected in the theme, only the desktop pill changed,
 * and the phone needed its own fix afterwards. One component, one fix.
 */

/** Two letters, the way initials are written on a badge. */
function initials(name?: string | null, fallback = '??') {
  const parts = String(name ?? '')
    .split(/\s+/)
    .filter(Boolean)

  if (parts.length === 0) {
    return fallback
  }

  return (parts.length === 1 ? parts[0].slice(0, 2) : parts[0][0] + parts[1][0]).toUpperCase()
}

/**
 * A person, as a circle of initials.
 *
 * Weight 600 rather than 700: Inter is bundled at 400, 500 and 600, so 700 was
 * a bold the browser had to invent.
 */
export function UserAvatar({
  name,
  size = 36,
  fallback,
}: {
  name?: string | null
  size?: number
  fallback?: string
}) {
  return (
    <Avatar
      sx={{
        width: size,
        height: size,
        bgcolor: M3.secondaryContainer,
        color: M3.primary,
        fontFamily: MONO,
        fontSize: size <= 32 ? '0.75rem' : '0.8125rem',
        fontWeight: 600,
      }}
    >
      {initials(name, fallback)}
    </Avatar>
  )
}

/**
 * A machine's condition, in the tone that condition carries.
 *
 * Not a MUI Chip: the phone header needs a denser pill than the fleet list, and
 * the two used to diverge into separate implementations over exactly that. The
 * colours come from the same theme helper the fleet chips use, so a machine
 * reads identically wherever it appears.
 */
export function StatusPill({ status }: { status?: string | null }) {
  const tone = statusPillColors(status)

  return (
    <Box
      className="status-pill"
      sx={{
        fontFamily: MONO,
        fontSize: '0.6875rem',
        lineHeight: '16px',
        fontWeight: 500,
        bgcolor: tone.bg,
        color: tone.fg,
        px: 1.25,
        py: 0.5,
        borderRadius: 999,
        whiteSpace: 'nowrap',
      }}
    >
      {displayLabel(status, 'Loading')}
    </Box>
  )
}
