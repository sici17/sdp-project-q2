import { createTheme } from '@mui/material/styles'

/**
 * "Fleet Precision" - the Material 3 system in
 * `stitch_material_manual_assistant/DESIGN.md`, expressed as an MUI theme.
 *
 * The tokens below are that file's, not invented here: the palette is its M3
 * tonal roles, the type scale its three families, the radii its shape scale. A
 * value that does not appear in DESIGN.md should not appear here either.
 *
 * All three families are bundled rather than linked from Google Fonts. The page
 * is served under `default-src 'self'`, so a CDN stylesheet is simply blocked -
 * which is how the type silently fell back to Helvetica before.
 */

/** The M3 colour roles from DESIGN.md, kept under their own names. */
export const M3 = {
  surface: '#f8f9fc',
  surfaceDim: '#d9dadd',
  surfaceContainerLowest: '#ffffff',
  surfaceContainerLow: '#f2f3f6',
  surfaceContainer: '#edeef1',
  surfaceContainerHigh: '#e7e8eb',
  surfaceContainerHighest: '#e1e2e5',
  onSurface: '#191c1e',
  onSurfaceVariant: '#414751',
  outline: '#727783',
  outlineVariant: '#c1c6d3',
  primary: '#004786',
  onPrimary: '#ffffff',
  primaryContainer: '#005faf',
  onPrimaryContainer: '#c4daff',
  secondary: '#565e71',
  secondaryContainer: '#dae2f9',
  onSecondaryContainer: '#5c6478',
  error: '#ba1a1a',
  onError: '#ffffff',
  errorContainer: '#ffdad6',
  onErrorContainer: '#93000a',
  background: '#f8f9fc',
} as const

const HEADING = '"Hanken Grotesk", "Segoe UI", Arial, sans-serif'
const BODY = 'Inter, "Segoe UI", Arial, sans-serif'
/** Machine IDs, serials and alarm codes, so alphanumerics are easy to compare. */
export const MONO = '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace'

/**
 * Material 3's emphasized motion, for the panels that slide in.
 *
 * The default is a symmetric ease that makes a full-height panel feel like it
 * is being dragged. Entering decelerates hard from an over-fast start, leaving
 * accelerates away and takes half as long - so opening reads as arrival and
 * closing does not make you wait for it.
 */
export const MOTION = {
  easing: {
    enter: 'cubic-bezier(0.05, 0.7, 0.1, 1)',
    exit: 'cubic-bezier(0.3, 0, 0.8, 0.15)',
  },
  duration: { enter: 400, exit: 200 },
} as const

export const theme = createTheme({
  palette: {
    mode: 'light',
    primary: {
      main: M3.primary,
      light: M3.primaryContainer,
      contrastText: M3.onPrimary,
    },
    secondary: { main: M3.secondary, light: M3.secondaryContainer },
    error: { main: M3.error, light: M3.errorContainer, contrastText: M3.onError },
    warning: { main: '#8a5000' },
    success: { main: '#1e6b3a' },
    info: { main: M3.primaryContainer },
    background: { default: M3.background, paper: M3.surfaceContainerLowest },
    text: { primary: M3.onSurface, secondary: M3.onSurfaceVariant },
    divider: M3.outlineVariant,
  },
  shape: { borderRadius: 8 },
  typography: {
    fontFamily: BODY,
    // Headlines are Hanken Grotesk; they scale down on mobile so a title does
    // not wrap awkwardly in a narrow sidebar or machine card.
    h1: { fontFamily: HEADING, fontSize: '2rem', fontWeight: 600, lineHeight: '40px' },
    h2: { fontFamily: HEADING, fontSize: '1.5rem', fontWeight: 600, lineHeight: '32px' },
    h3: { fontFamily: HEADING, fontSize: '1.125rem', fontWeight: 600, lineHeight: '24px' },
    subtitle1: { fontFamily: BODY, fontSize: '1rem', fontWeight: 500, lineHeight: '24px' },
    subtitle2: { fontFamily: BODY, fontSize: '0.875rem', fontWeight: 600 },
    body1: { fontSize: '1rem', lineHeight: '24px' },
    body2: { fontSize: '0.875rem', lineHeight: '20px' },
    caption: { fontSize: '0.75rem', lineHeight: '16px' },
    // label-lg / label-md in DESIGN.md: monospace, for data rather than prose.
    overline: {
      fontFamily: MONO,
      fontSize: '0.6875rem',
      fontWeight: 500,
      lineHeight: '16px',
      letterSpacing: '0.04em',
    },
    button: { fontFamily: BODY, fontWeight: 500 },
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        // A narrow, unobtrusive scrollbar; the panes scroll independently and
        // three default bars side by side dominated the layout.
        '*::-webkit-scrollbar': { width: 8, height: 8 },
        '*::-webkit-scrollbar-thumb': { background: M3.outlineVariant, borderRadius: 8 },
        '*::-webkit-scrollbar-track': { background: 'transparent' },
        // No 300ms wait before a tap registers as a click.
        'button, a, label, [role="button"], summary': { touchAction: 'manipulation' },
        /*
          Honour a system request for less motion.

          The panels slide, the sheet is dragged to height and the chat scrolls
          itself - all of which this turns off. `!important` is deliberate:
          MUI's transitions are written to the element's style attribute, and a
          plain rule in a stylesheet does not outrank an inline one.
        */
        '@media (prefers-reduced-motion: reduce)': {
          '*, *::before, *::after': {
            animationDuration: '0.01ms !important',
            animationIterationCount: '1 !important',
            transitionDuration: '0.01ms !important',
            scrollBehavior: 'auto !important',
          },
        },
      },
    },
    MuiButton: {
      defaultProps: { variant: 'contained', disableElevation: true },
      styleOverrides: {
        // Buttons and chips take the full radius so they read as interactive
        // against the 8px containers. 44px is the smallest target reliably hit
        // with a gloved thumb.
        root: { minHeight: 44, textTransform: 'none', borderRadius: 999, paddingInline: 20 },
        sizeSmall: { minHeight: 36, paddingInline: 14 },
      },
    },
    MuiIconButton: { styleOverrides: { root: { minWidth: 44, minHeight: 44 } } },
    MuiTextField: { defaultProps: { size: 'small', fullWidth: true } },
    MuiOutlinedInput: {
      styleOverrides: { root: { borderRadius: 8, background: M3.surfaceContainerLowest } },
    },
    MuiCard: {
      defaultProps: { elevation: 0 },
      styleOverrides: {
        root: {
          borderRadius: 12,
          border: `1px solid ${M3.outlineVariant}80`,
          boxShadow: '0 1px 4px rgba(25, 28, 30, 0.05)',
        },
      },
    },
    MuiPaper: { styleOverrides: { rounded: { borderRadius: 12 } } },
    MuiChip: {
      styleOverrides: {
        root: { fontFamily: MONO, fontWeight: 500, borderRadius: 999 },
        sizeSmall: { height: 24, fontSize: '0.6875rem' },
        /*
          Scoped to the default colour on purpose.

          Unscoped, this painted every filled chip neutral - so a machine in
          alarm and a machine running normally carried visually identical grey
          chips on the fleet list, and the one signal that screen exists to give
          was not being given. A chip that was asked for a semantic colour keeps
          it; only an unqualified chip falls back to the surface tone.
        */
        filled: {
          '&.MuiChip-colorDefault': {
            background: M3.surfaceContainerHighest,
            color: M3.onSurfaceVariant,
          },
          '&.MuiChip-colorError': { background: M3.errorContainer, color: M3.onErrorContainer },
          '&.MuiChip-colorSuccess': { background: '#c8e6cd', color: '#0b3d1f' },
          '&.MuiChip-colorWarning': { background: '#ffdfa8', color: '#5a3300' },
          '&.MuiChip-colorInfo': { background: M3.secondaryContainer, color: M3.onSurfaceVariant },
        },

      },
    },
    MuiAlert: {
      styleOverrides: {
        // Alarms do not rely on shadow: a stroke of the semantic colour and a
        // tonal tint keep them visible at low screen brightness.
        root: {
          borderRadius: 12,
          alignItems: 'center',
          border: '1px solid transparent',
          '&.MuiAlert-standardError': {
            background: M3.errorContainer,
            color: M3.onErrorContainer,
            borderColor: M3.error,
            borderLeftWidth: 4,
          },
        },
      },
    },
    MuiAccordion: {
      styleOverrides: {
        root: {
          borderRadius: 12,
          border: `1px solid ${M3.outlineVariant}80`,
          '&::before': { display: 'none' },
          '&.Mui-expanded': { margin: 0 },
        },
      },
    },
    MuiAppBar: {
      styleOverrides: {
        root: {
          background: M3.surface,
          color: M3.onSurface,
          borderBottom: `1px solid ${M3.outlineVariant}4d`,
        },
      },
    },
    MuiToolbar: { styleOverrides: { root: { minHeight: 64 } } },
    MuiDrawer: {
      styleOverrides: {
        paper: { background: M3.surface, borderRight: `1px solid ${M3.outlineVariant}4d` },
      },
    },
    MuiListItemButton: {
      styleOverrides: {
        root: {
          borderRadius: 999,
          minHeight: 48,
          '&.Mui-selected': {
            background: M3.secondaryContainer,
            color: M3.primary,
            '&:hover': { background: M3.secondaryContainer },
          },
        },
      },
    },
  },
})

/** Material's severity role for a machine or telemetry status. */
/**
 * Tonal colours for the machine status pill.
 *
 * The pill was painted a fixed surface tone, so "critical" and "ok" were the
 * same grey and the header reported a state without signalling it. Same tones
 * as the fleet list chips, so one machine reads identically in both places.
 */
/**
 * A raw enum value, as a label.
 *
 * `critical`, `full`, `technician` and `commercial` are how the dataset stores
 * them and how the access model compares them. They are not how a sentence
 * starts, and they were reaching the screen unchanged.
 */
export function displayLabel(value?: string | null, fallback = '') {
  const text = (value ?? '').trim()
  if (!text) {
    return fallback
  }

  return `${text[0].toUpperCase()}${text.slice(1)}`
}

export function statusPillColors(status?: string | null) {
  switch (status) {
    case 'critical':
      return { bg: M3.errorContainer, fg: M3.onErrorContainer }
    case 'warning':
      return { bg: '#ffdfa8', fg: '#5a3300' }
    case 'ok':
    case 'healthy':
      return { bg: '#c8e6cd', fg: '#0b3d1f' }
    default:
      return { bg: M3.surfaceContainerHigh, fg: M3.onSurfaceVariant }
  }
}

export function statusSeverity(status?: string | null) {
  switch (status) {
    case 'critical':
      return 'error' as const
    case 'warning':
      return 'warning' as const
    case 'ok':
    case 'healthy':
      return 'success' as const
    default:
      return 'info' as const
  }
}
