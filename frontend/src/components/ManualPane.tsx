import Box from '@mui/material/Box'
import Paper from '@mui/material/Paper'
import Typography from '@mui/material/Typography'

interface ManualPaneProps {
  manualUrl: string | null
  iframeTitle: string
}

/**
 * The machine's own use-and-maintenance manual.
 *
 * The desktop surface intentionally contains only the issued PDF. Its native
 * toolbar already provides page and zoom controls, so a second title, search
 * status, and page form above it only duplicated information and reduced the
 * document's working area.
 */
export function ManualPane({ manualUrl, iframeTitle }: ManualPaneProps) {
  return (
    <Paper
      id="manual-panel"
      component="section"
      aria-label="Manual"
      variant="outlined"
      sx={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}
    >
      <Box sx={{ flex: 1, minHeight: 320, bgcolor: 'grey.100' }}>
        {manualUrl ? (
          <Box
            component="iframe"
            key={manualUrl}
            src={manualUrl}
            title={iframeTitle}
            sx={{ width: '100%', height: '100%', border: 0, display: 'block' }}
          />
        ) : (
          <Box sx={{ p: 4, textAlign: 'center' }}>
            <Typography color="text.secondary">
              No manual is available for this machine.
            </Typography>
          </Box>
        )}
      </Box>
    </Paper>
  )
}
