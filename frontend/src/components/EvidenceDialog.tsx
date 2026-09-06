import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Card from '@mui/material/Card'
import CardContent from '@mui/material/CardContent'
import Chip from '@mui/material/Chip'
import Dialog from '@mui/material/Dialog'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import DialogTitle from '@mui/material/DialogTitle'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'

import type { Evidence } from '../types/contracts'

interface EvidenceDialogProps {
  open: boolean
  heading: string
  evidence: Evidence[]
  isCurrent: (item: Evidence) => boolean
  citationHref: (item: Evidence) => string | null
  onShowCitation: (item: Evidence) => void
  onClose: () => void
}

/**
 * The passages an answer was built from.
 *
 * A Material Dialog rather than a hand-built modal: focus trapping, restore on
 * close, Escape and the scrim all come from the component, which is most of
 * what the previous implementation had to keep in its own refs and effects.
 */
export function EvidenceDialog({
  open,
  heading,
  evidence,
  isCurrent,
  citationHref,
  onShowCitation,
  onClose,
}: EvidenceDialogProps) {
  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth scroll="paper">
      <DialogTitle>
        <Typography variant="overline" color="text.secondary" sx={{ display: 'block' }}>
          {heading}
        </Typography>
        {evidence.length} source{evidence.length === 1 ? '' : 's'}
      </DialogTitle>

      <DialogContent dividers>
        {evidence.length === 0 ? (
          <Typography color="text.secondary">No sources available yet.</Typography>
        ) : (
          <Stack spacing={2}>
            {evidence.map((item) => {
              const citation = citationHref(item)
              const current = isCurrent(item)

              return (
                <Card key={`${item.source}-${item.title}-${item.chunkId ?? item.page}`}>
                  <CardContent>
                    <Stack
                      direction="row"
                      spacing={1}
                      sx={{ alignItems: 'center', flexWrap: 'wrap', mb: 1 }}
                    >
                      <Chip
                        size="small"
                        label={item.source}
                        color={current ? 'primary' : 'default'}
                        variant={current ? 'filled' : 'outlined'}
                      />
                      {current ? <Chip size="small" label="current" color="primary" /> : null}
                      {item.page ? (
                        <Chip
                          size="small"
                          variant="outlined"
                          label={`Page ${item.printedPage ?? item.page}`}
                        />
                      ) : null}
                      {item.manualVersion ? (
                        <Chip size="small" variant="outlined" label={`Manual v${item.manualVersion}`} />
                      ) : null}
                    </Stack>

                    <Typography variant="subtitle2" gutterBottom>
                      {item.section ?? item.title}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      {item.excerpt}
                    </Typography>

                    {citation ? (
                      <Box sx={{ mt: 1.5, display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                        {item.page ? (
                          <Button size="small" variant="outlined" onClick={() => onShowCitation(item)}>
                            Show page {item.printedPage ?? item.page}
                          </Button>
                        ) : null}
                        <Link href={citation} target="_blank" rel="noreferrer" variant="body2">
                          Open source
                        </Link>
                      </Box>
                    ) : null}
                  </CardContent>
                </Card>
              )
            })}
          </Stack>
        )}
      </DialogContent>

      <DialogActions>
        <Button autoFocus onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  )
}
