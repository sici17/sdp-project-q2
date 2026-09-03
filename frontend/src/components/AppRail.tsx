import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Tooltip from '@mui/material/Tooltip'

import { M3, MONO } from '../theme'

interface AppRailProps {
  expanded: boolean
  onToggle: () => void
  active: 'fleet' | 'tickets' | 'quotes'
  onSelect: (target: AppRailProps['active']) => void
}

/** The context destinations that add information beyond the manual already on screen. */
const DESTINATIONS = [
  { id: 'fleet', short: 'M', label: 'Machine' },
  { id: 'tickets', short: 'T', label: 'Tickets' },
  { id: 'quotes', short: 'Q', label: 'Quotations' },
] as const

/** The three-bar glyph, drawn rather than pulled from an icon font. */
function MenuGlyph() {
  return (
    <Box sx={{ display: 'grid', gap: '4px' }}>
      {[0, 1, 2].map((bar) => (
        <Box key={bar} sx={{ width: 16, height: 2, bgcolor: '#fff' }} />
      ))}
    </Box>
  )
}

/**
 * The 64px navigation rail.
 *
 * The machine's own context has moved into the header, so the rail no longer
 * has to be 300px wide just to hold it. Expanding it brings the full context
 * panel back for the cases that need the detail.
 */
export function AppRail({ expanded, onToggle, active, onSelect }: AppRailProps) {
  return (
    <Box
      component="nav"
      aria-label="Sections"
      sx={{
        width: 64,
        flex: 'none',
        bgcolor: M3.primary,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        py: 1.75,
        gap: 1.25,
      }}
    >
      <ButtonBase
        onClick={onToggle}
        aria-label={expanded ? 'Collapse machine context' : 'Expand machine context'}
        aria-expanded={expanded}
        aria-controls="machine-context-panel"
        sx={{
          width: 40,
          height: 40,
          borderRadius: '10px',
          bgcolor: 'rgba(255, 255, 255, .16)',
        }}
      >
        <MenuGlyph />
      </ButtonBase>

      {DESTINATIONS.map((destination) => {
        const isActive = destination.id === active

        return (
          <Tooltip key={destination.id} title={destination.label} placement="right">
            <ButtonBase
              onClick={() => onSelect(destination.id)}
              aria-label={destination.label}
              aria-current={isActive ? 'page' : undefined}
              sx={{
                width: 40,
                height: 40,
                borderRadius: '10px',
                fontFamily: MONO,
                fontSize: '0.625rem',
                fontWeight: 500,
                bgcolor: isActive ? '#fff' : 'transparent',
                color: isActive ? M3.primary : M3.onPrimaryContainer,
                '&:hover': { bgcolor: isActive ? '#fff' : 'rgba(255, 255, 255, .12)' },
              }}
            >
              {destination.short}
            </ButtonBase>
          </Tooltip>
        )
      })}

      <Box sx={{ flex: 1 }} />
    </Box>
  )
}
