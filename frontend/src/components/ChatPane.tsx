import { UserAvatar } from './Identity'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import type { FormEvent, RefObject } from 'react'
import Accordion from '@mui/material/Accordion'
import AccordionDetails from '@mui/material/AccordionDetails'
import AccordionSummary from '@mui/material/AccordionSummary'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import Divider from '@mui/material/Divider'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'

import { DiagnosticStepList } from './DiagnosticSteps'
import { MarkdownMessage } from './MarkdownMessage'
import { M3, MONO } from '../theme'
import type { ChatMessage, DiagnosticStep, Evidence } from '../types/contracts'

interface ChatPaneProps {
  messages: ChatMessage[]
  messageEvidence: Record<string, Evidence[]>
  chatListRef: RefObject<HTMLDivElement | null>
  evidence: Evidence[]
  onOpenEvidence: (messageId: string | null) => void

  diagnosticSteps: DiagnosticStep[]
  diagnosticChecks: Record<string, boolean>
  diagnosticKey: (step: DiagnosticStep, index: number) => string
  isDiagnosticOpen: boolean
  onDiagnosticOpenChange: (open: boolean) => void
  onDiagnosticFollowUp: (step: DiagnosticStep, index: number, outcome: 'completed' | 'failed') => void

  retryableSubmission: boolean
  onRetrySubmission: () => void
  onReloadWorkspace: () => void

  quickPrompts: string[]
  onQuickPrompt: (prompt: string) => void

  attachmentSize: (bytes: number) => string

  draft: string
  onDraftChange: (value: string) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  isSending: boolean
  isInitializing: boolean
  disabled: boolean
}

/**
 * The assistant, beside the manual it cites.
 *
 * Every answer keeps its own sources: the button under a message opens the
 * passages that answer was built from, rather than a single shared list that
 * has already moved on to the next question.
 */
export function ChatPane({
  messages,
  messageEvidence,
  chatListRef,
  evidence,
  onOpenEvidence,
  diagnosticSteps,
  diagnosticChecks,
  diagnosticKey,
  isDiagnosticOpen,
  onDiagnosticOpenChange,
  onDiagnosticFollowUp,
  retryableSubmission,
  onRetrySubmission,
  onReloadWorkspace,
  quickPrompts,
  onQuickPrompt,
  attachmentSize,
  draft,
  onDraftChange,
  onSubmit,
  isSending,
  isInitializing,
  disabled,
}: ChatPaneProps) {
  return (
    <Paper
      id="chat-panel"
      component="section"
      aria-label="Machine support"
      variant="outlined"
      sx={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}
    >
      <Stack
        direction="row"
        spacing={1.5}
        sx={{ alignItems: 'center', px: 2, py: 1.5, borderBottom: '1px solid', borderColor: 'divider' }}
      >
        <UserAvatar name="Service Assist" size={32} />
        <Typography variant="h3">Service Assist</Typography>
      </Stack>

      <Box
        ref={chatListRef}
        className="chat-list"
        sx={{ flex: 1, minHeight: 0, overflowY: 'auto', overscrollBehavior: 'contain' }}
      >
        <Box role="log" aria-live="polite" sx={{ p: 2 }}>
          <Stack spacing={2}>
            {messages.map((message) => {
              const isOperator = message.role === 'user'
              const sources = messageEvidence[message.id] ?? []
              const isPending = message.role === 'assistant' && !message.content && isSending

              return (
                <Box
                  key={message.id}
                  className={`message ${message.role}`}
                  sx={{ display: 'flex', justifyContent: isOperator ? 'flex-end' : 'flex-start' }}
                >
                  <Paper
                    elevation={0}
                    sx={{
                      p: 2,
                      maxWidth: '92%',
                      minWidth: 0,
                      border: '1px solid',
                      borderColor: isOperator ? 'transparent' : `${M3.outlineVariant}40`,
                      bgcolor: isOperator ? M3.primary : M3.surfaceContainerLow,
                      color: isOperator ? M3.onPrimary : 'text.primary',
                      borderRadius: 3,
                      borderTopLeftRadius: isOperator ? 12 : 4,
                      borderTopRightRadius: isOperator ? 4 : 12,
                    }}
                  >
                    {!isOperator ? (
                      <Typography
                        sx={{
                          fontFamily: MONO,
                          fontSize: '0.6875rem',
                          color: 'text.secondary',
                          mb: 0.5,
                        }}
                      >
                        Service Assist
                      </Typography>
                    ) : null}

                    {isPending ? (
                      <Stack
                        direction="row"
                        spacing={1}
                        role="status"
                        aria-label="Searching manuals and machine records"
                        sx={{ alignItems: 'center', py: 0.5 }}
                      >
                        <CircularProgress size={16} thickness={5} />
                        <Typography variant="body2" color="text.secondary">
                          Searching manuals and machine records…
                        </Typography>
                      </Stack>
                    ) : message.role === 'assistant' ? (
                      <MarkdownMessage content={message.content || '...'} />
                    ) : (
                      <Typography variant="body2">{message.content || '...'}</Typography>
                    )}

                    {message.attachments?.length ? (
                      <Stack
                        direction="row"
                        spacing={0.5}
                        aria-label="Message attachments"
                        sx={{ flexWrap: 'wrap', mt: 1 }}
                      >
                        {message.attachments.map((attachment) => (
                          <Chip
                            key={attachment.id}
                            size="small"
                            variant="outlined"
                            label={`${attachment.name} (${attachmentSize(attachment.size)})`}
                          />
                        ))}
                      </Stack>
                    ) : null}

                    {message.role === 'assistant' && sources.length ? (
                      <Button
                        size="small"
                        variant="text"
                        sx={{ mt: 1 }}
                        onClick={() => onOpenEvidence(message.id)}
                      >
                        View {sources.length} source{sources.length === 1 ? '' : 's'}
                      </Button>
                    ) : null}
                  </Paper>
                </Box>
              )
            })}

            {isInitializing ? (
              <Stack
                direction="row"
                spacing={1}
                role="status"
                aria-label="Preparing machine data and chat"
                sx={{ alignItems: 'center', color: 'text.secondary' }}
              >
                <CircularProgress size={16} thickness={5} />
                <Typography variant="body2">Preparing machine data and chat…</Typography>
              </Stack>
            ) : null}
          </Stack>
        </Box>

        <Divider />

        <Box sx={{ px: 2, py: 1 }}>
          <Button
            size="small"
            variant="outlined"
            onClick={() => onOpenEvidence(null)}
            disabled={evidence.length === 0}
          >
            Sources{evidence.length > 0 ? ` (${evidence.length})` : ''}
          </Button>
        </Box>

        <Accordion
          expanded={isDiagnosticOpen}
          onChange={(_event, expanded) => onDiagnosticOpenChange(expanded)}
          disableGutters
          elevation={0}
          square
        >
        <AccordionSummary
          aria-label="Troubleshooting"
          expandIcon={<ExpandMoreIcon />}
          sx={{ minHeight: 48, '& .MuiAccordionSummary-content': { alignItems: 'center', gap: 1 } }}
        >
          <Typography sx={{ flexGrow: 1 }}>Troubleshooting</Typography>
          <Typography variant="body2" color="text.secondary">
            {diagnosticSteps.length ? `${diagnosticSteps.length} steps` : 'None yet'}
          </Typography>
        </AccordionSummary>
        <AccordionDetails>
          <DiagnosticStepList
            steps={diagnosticSteps}
            checks={diagnosticChecks}
            stepKey={diagnosticKey}
            disabled={disabled}
            onFollowUp={onDiagnosticFollowUp}
          />
        </AccordionDetails>
        </Accordion>

        {retryableSubmission ? (
          <Alert
            severity="warning"
            sx={{ mx: 2, mb: 1 }}
            action={
              <Stack direction="row" spacing={1}>
                <Button size="small" onClick={onRetrySubmission} disabled={isSending}>
                  Try again
                </Button>
                <Button size="small" variant="text" onClick={onReloadWorkspace} disabled={isSending}>
                  Reload chat
                </Button>
              </Stack>
            }
          >
            Delivery could not be confirmed. You can try the message again.
          </Alert>
        ) : null}

        <Stack direction="row" spacing={1} sx={{ px: 2, pb: 1.5, flexWrap: 'wrap', gap: 1 }}>
          {quickPrompts.map((prompt) => (
            <Chip
              key={prompt}
              label={prompt}
              variant="outlined"
              onClick={() => onQuickPrompt(prompt)}
              disabled={disabled}
              sx={{
                fontFamily: 'inherit',
                height: 'auto',
                py: 0.75,
                borderColor: M3.outlineVariant,
                '& .MuiChip-label': { whiteSpace: 'normal', fontSize: '0.8125rem' },
              }}
            />
          ))}
        </Stack>
      </Box>

      <Divider />

      <Box
        component="form"
        className="chat-form"
        onSubmit={onSubmit}
        sx={{ flex: 'none', p: 2, bgcolor: M3.surfaceContainerLowest, position: 'relative', zIndex: 1 }}
      >
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <TextField
            value={draft}
            onChange={(event) => onDraftChange(event.target.value)}
            placeholder="Ask about the manual, telemetry, alarms, or service status"
            slotProps={{ htmlInput: { maxLength: 2000, 'aria-label': 'Message', autoComplete: 'off' } }}
            disabled={disabled}
            // The input stops accepting keystrokes at the cap. Without a count
            // that reads as a broken keyboard rather than a limit.
            helperText={draft.length > 1800 ? `${draft.length}/2000` : ' '}
          />
          <Button
            type="submit"
            disabled={disabled}
            aria-label={isSending ? 'Sending message' : 'Send'}
            sx={{ flex: 'none', minWidth: 88 }}
          >
            {isSending ? <CircularProgress size={16} color="inherit" sx={{ mr: 1 }} /> : null}
            Send
          </Button>
        </Stack>
      </Box>
    </Paper>
  )
}
