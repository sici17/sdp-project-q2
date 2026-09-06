import { StatusPill } from './Identity'
import ArrowUpwardIcon from '@mui/icons-material/ArrowUpward'
import ChevronRightIcon from '@mui/icons-material/ChevronRight'
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined'
import MenuIcon from '@mui/icons-material/Menu'
import { useState, type FormEvent, type RefObject } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import IconButton from '@mui/material/IconButton'
import InputBase from '@mui/material/InputBase'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'

import { DiagnosticStepList } from './DiagnosticSteps'
import { MarkdownMessage } from './MarkdownMessage'
import { M3, MONO } from '../theme'
import type {
  ChatMessage,
  DiagnosticStep,
  Evidence,
  Machine,
  TelemetrySnapshot,
} from '../types/contracts'

interface MobileWorkspaceProps {
  machine: Machine | null
  machineId: string
  telemetry: TelemetrySnapshot | null
  messages: ChatMessage[]
  messageEvidence: Record<string, Evidence[]>
  chatListRef: RefObject<HTMLDivElement | null>
  quickPrompts: string[]
  diagnosticSteps: DiagnosticStep[]
  diagnosticChecks: Record<string, boolean>
  diagnosticKey: (step: DiagnosticStep, index: number) => string
  onDiagnosticFollowUp: (step: DiagnosticStep, index: number, outcome: 'completed' | 'failed') => void
  draft: string
  isSending: boolean
  isInitializing: boolean
  disabled: boolean
  isContextOpen: boolean
  retryableSubmission: boolean
  onRetrySubmission: () => void
  onReloadWorkspace: () => void
  attachmentSize: (bytes: number) => string
  formatRate: (telemetry?: TelemetrySnapshot | null) => string
  onOpenContext: () => void
  onOpenManual: () => void
  onOpenEvidence: (messageId: string) => void
  onQuickPrompt: (prompt: string) => void
  onDraftChange: (value: string) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
}

/** The smallest target a gloved thumb hits reliably. */
const TOUCH = 48

/**
 * An alarm mnemonic, read out as words.
 *
 * `AL019_CAPS_SORTER_UPPER_DOOR_OPEN` is precise but it is not a sentence, and
 * the operator holding the phone is being told what is wrong, not being quizzed
 * on the code. The code itself stays on screen directly above this.
 */
function readAlarm(code: string) {
  const words = code
    .replace(/^AL\d+_/i, '')
    .replace(/_/g, ' ')
    .toLowerCase()
    .trim()

  return words ? words.charAt(0).toUpperCase() + words.slice(1) : code
}

/** The pinned two-line machine strip, with the machine's condition under it. */
function MachineStrip({
  machine,
  machineId,
  telemetry,
  isContextOpen,
  formatRate,
  onOpenContext,
}: Pick<
  MobileWorkspaceProps,
  'machine' | 'machineId' | 'telemetry' | 'isContextOpen' | 'formatRate' | 'onOpenContext'
>) {
  const subtitle = [
    machine?.serialNumber ? `SERIAL ${machine.serialNumber}` : null,
    machine?.plant || null,
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <Box
      component="header"
      className="workspace-header"
      sx={{
        flex: 'none',
        bgcolor: M3.surfaceContainerLowest,
        borderBottom: '1px solid',
        borderColor: M3.surfaceContainerHighest,
        px: 2,
        pt: 1,
        pb: 1.25,
      }}
    >
      <ButtonBase
        onClick={onOpenContext}
        aria-label="Open machine context"
        aria-expanded={isContextOpen}
        aria-controls="machine-context-panel"
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 1.5,
          width: '100%',
          minHeight: TOUCH,
          textAlign: 'left',
          borderRadius: 2,
        }}
      >
        <Box
          data-testid="machine-menu-icon"
          aria-hidden="true"
          sx={{
            width: 44,
            height: 44,
            borderRadius: 999,
            bgcolor: M3.surfaceContainerLow,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '4px',
            flex: 'none',
          }}
        >
          <MenuIcon sx={{ color: M3.onSurfaceVariant, fontSize: '1.375rem' }} />
        </Box>

        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography
            noWrap
            sx={{ fontFamily: MONO, fontSize: '0.9375rem', lineHeight: '20px', fontWeight: 600 }}
          >
            {machine?.model ?? machine?.id ?? machineId}
          </Typography>
          <Typography
            noWrap
            sx={{ fontFamily: MONO, fontSize: '0.6875rem', lineHeight: '16px', color: M3.outline }}
          >
            {subtitle || 'Loading machine'}
          </Typography>
        </Box>

        <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flex: 'none' }}>
          <StatusPill status={machine?.status} />
          <ChevronRightIcon sx={{ color: M3.onSurfaceVariant, fontSize: '1.25rem' }} />
        </Stack>
      </ButtonBase>

      {/*
        Condition, without opening anything: scanning the code on a machine is a
        question about how it is running.
      */}
      <Box component="section" aria-label="Machine condition" sx={{ mt: 1.25 }}>
        {telemetry?.activeAlarm ? (
          <Stack
            direction="row"
            spacing={1.25}
            role="status"
            sx={{
              alignItems: 'center',
              bgcolor: M3.errorContainer,
              borderLeft: `4px solid ${M3.error}`,
              borderRadius: 2,
              px: 1.25,
              py: 1,
            }}
          >
            <Box
              sx={{
                width: 16,
                height: 16,
                borderRadius: 999,
                border: `2px solid ${M3.onErrorContainer}`,
                flex: 'none',
              }}
            />
            <Box sx={{ flex: 1, minWidth: 0 }}>
              {/*
                The code on a full-width line of its own. Beside the rate in a
                tile it was clipped to "AL019_CAPS_SORTI".
              */}
              <Typography
                className="condition-alarm"
                sx={{
                  fontFamily: MONO,
                  fontSize: '0.6875rem',
                  lineHeight: '14px',
                  fontWeight: 500,
                  color: M3.onErrorContainer,
                }}
              >
                {telemetry.activeAlarm}
              </Typography>
              <Typography
                sx={{
                  fontFamily: MONO,
                  fontSize: '0.6875rem',
                  lineHeight: '14px',
                  color: M3.onErrorContainer,
                  opacity: 0.8,
                }}
              >
                {formatRate(telemetry)}
              </Typography>
            </Box>
          </Stack>
        ) : (
          <Typography
            role="status"
            sx={{ fontFamily: MONO, fontSize: '0.6875rem', color: M3.outline }}
          >
              {telemetry ? `No active alarm · ${formatRate(telemetry)}` : 'Machine condition unavailable'}
          </Typography>
        )}
      </Box>
    </Box>
  )
}

/**
 * The phone layout: one column, chat first.
 *
 * A machine is scanned standing in front of it, so the question - not the
 * document - is what the screen opens on. The manual is one tap away and comes
 * back as a sheet over the conversation rather than a page that replaces it.
 */
export function MobileWorkspace({
  machine,
  machineId,
  telemetry,
  messages,
  messageEvidence,
  chatListRef,
  quickPrompts,
  diagnosticSteps,
  diagnosticChecks,
  diagnosticKey,
  onDiagnosticFollowUp,
  draft,
  isSending,
  isInitializing,
  disabled,
  isContextOpen,
  retryableSubmission,
  onRetrySubmission,
  onReloadWorkspace,
  attachmentSize,
  formatRate,
  onOpenContext,
  onOpenManual,
  onOpenEvidence,
  onQuickPrompt,
  onDraftChange,
  onSubmit,
}: MobileWorkspaceProps) {
  // The suggestions are an opening move, not a permanent menu: once there is a
  // conversation they are three rows in the way of it.
  const showSuggestions = messages.every((message) => message.role !== 'user')

  return (
    <Box
      className="workspace"
      sx={{ display: 'flex', flexDirection: 'column', height: '100dvh', overflow: 'hidden' }}
    >
      <MachineStrip
        machine={machine}
        machineId={machineId}
        telemetry={telemetry}
        isContextOpen={isContextOpen}
        formatRate={formatRate}
        onOpenContext={onOpenContext}
      />

      {/*
        The conversation and the box you answer it in are one surface. The log
        role sits on the scroller inside it, because on the section itself it
        would replace the region landmark rather than sit within it.
      */}
      <Box
        component="section"
        aria-label="Machine support"
        sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
      >
        <Box
          ref={chatListRef}
          className="chat-list"
          role="log"
          aria-live="polite"
          sx={{
            flex: 1,
            minHeight: 0,
            overflowY: 'auto',
            p: 2,
            display: 'flex',
            flexDirection: 'column',
            gap: 1.75,
          }}
        >
          {messages.map((message) => {
            const isOperator = message.role === 'user'
            const sources = messageEvidence[message.id] ?? []
            const isPending = message.role === 'assistant' && !message.content && isSending

            if (isOperator) {
              return (
                <Box
                  key={message.id}
                  className={`message ${message.role}`}
                  sx={{
                    alignSelf: 'flex-end',
                    maxWidth: '78%',
                    bgcolor: M3.primary,
                    color: M3.onPrimary,
                    borderRadius: '12px 12px 4px 12px',
                    px: 1.75,
                    py: 1.5,
                    fontSize: '0.875rem',
                    lineHeight: '21px',
                  }}
                >
                  {message.content || '…'}
                </Box>
              )
            }

            return (
              <Stack
                key={message.id}
                direction="row"
                spacing={1.25}
                className={`message ${message.role}`}
                sx={{ alignItems: 'flex-start' }}
              >
                <Box
                  sx={{
                    width: 28,
                    height: 28,
                    borderRadius: 999,
                    bgcolor: M3.primary,
                    color: M3.onPrimary,
                    fontFamily: MONO,
                    fontSize: '0.625rem',
                    fontWeight: 500,
                    display: 'grid',
                    placeItems: 'center',
                    flex: 'none',
                  }}
                >
                  FA
                </Box>
                <Paper
                  elevation={0}
                  sx={{
                    border: '1px solid',
                    borderColor: M3.surfaceContainerHighest,
                    borderRadius: 3,
                    px: 1.75,
                    py: 1.5,
                    minWidth: 0,
                  }}
                >
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
                  ) : (
                    <MarkdownMessage content={message.content || '…'} />
                  )}

                  {message.attachments?.length ? (
                    <Stack
                      direction="row"
                      spacing={0.5}
                      aria-label="Message attachments"
                      sx={{ flexWrap: 'wrap', gap: 0.5, mt: 1 }}
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

                  {sources.length ? (
                    <Button
                      size="small"
                      variant="text"
                      sx={{ mt: 0.5, px: 0 }}
                      onClick={() => onOpenEvidence(message.id)}
                    >
                      View {sources.length} source{sources.length === 1 ? '' : 's'}
                    </Button>
                  ) : null}
                </Paper>
              </Stack>
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

          {/*
            The alarm again, this time as the thing to act on. The strip above
            says what is wrong; this says what to do about it.
          */}
          {telemetry?.activeAlarm ? (
            <Paper
              elevation={0}
              aria-label="Active alarm"
              sx={{
                border: `2px solid ${M3.error}`,
                borderRadius: 3,
                p: 1.75,
                display: 'flex',
                flexDirection: 'column',
                gap: 1.25,
              }}
            >
              <Typography
                sx={{
                  fontFamily: MONO,
                  fontSize: '0.6875rem',
                  lineHeight: '16px',
                  fontWeight: 500,
                  color: M3.onErrorContainer,
                }}
              >
                ACTIVE ALARM
              </Typography>
              <Typography variant="h3" sx={{ fontSize: '1.125rem', lineHeight: '24px' }}>
                {readAlarm(telemetry.activeAlarm)}
              </Typography>
              <Stack direction="row" spacing={1.25} sx={{ mt: 0.25 }}>
                <AlarmWalkthroughButton
                  key={`${machineId}:${telemetry.activeAlarm}`}
                  disabled={disabled}
                  onClick={() => onQuickPrompt(`Walk me through clearing ${telemetry.activeAlarm}.`)}
                />
                <IconButton
                  onClick={onOpenContext}
                  aria-label="Machine details"
                  sx={{
                    width: TOUCH,
                    height: TOUCH,
                    border: `1px solid ${M3.outline}`,
                    color: M3.primary,
                  }}
                >
                  <InfoOutlinedIcon fontSize="small" />
                </IconButton>
              </Stack>
            </Paper>
          ) : null}

          {/*
            The checklist an operator is meant to work through, on the device
            they are holding while they do it. The phone renders its own
            workspace rather than the desktop chat pane, so these steps existed
            in the payload and were shown nowhere.
          */}
          {diagnosticSteps.length ? (
            <Stack spacing={1} component="section" aria-label="Troubleshooting">
              <Typography
                sx={{
                  fontFamily: MONO,
                  fontSize: '0.6875rem',
                  lineHeight: '16px',
                  fontWeight: 500,
                  color: M3.outline,
                }}
              >
                {`TROUBLESHOOTING · ${diagnosticSteps.length} STEPS`}
              </Typography>
              <DiagnosticStepList
                steps={diagnosticSteps}
                checks={diagnosticChecks}
                stepKey={diagnosticKey}
                disabled={disabled}
                onFollowUp={onDiagnosticFollowUp}
              />
            </Stack>
          ) : null}

          {showSuggestions ? (
            <Stack spacing={1} component="section" aria-label="Suggested questions">
              <Typography
                sx={{
                  fontFamily: MONO,
                  fontSize: '0.6875rem',
                  lineHeight: '16px',
                  fontWeight: 500,
                  color: M3.outline,
                }}
              >
                SUGGESTED
              </Typography>
              {quickPrompts.map((prompt) => (
                <ButtonBase
                  key={prompt}
                  onClick={() => onQuickPrompt(prompt)}
                  disabled={disabled}
                  sx={{
                    minHeight: 56,
                    bgcolor: M3.surfaceContainerLowest,
                    border: `1px solid ${M3.outlineVariant}`,
                    borderRadius: 3,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    px: 2,
                    fontSize: '0.875rem',
                    textAlign: 'left',
                  }}
                >
                  <Box component="span" sx={{ minWidth: 0, pr: 1 }}>
                    {prompt}
                  </Box>
                  <ChevronRightIcon sx={{ color: M3.outline, flex: 'none', fontSize: '1.25rem' }} />
                </ButtonBase>
              ))}
            </Stack>
          ) : null}

          {retryableSubmission ? (
            <Alert
              severity="warning"
              action={
                <Stack direction="row" spacing={1}>
                  <Button size="small" onClick={onRetrySubmission} disabled={isSending}>
                    Try again
                  </Button>
                  <Button
                    size="small"
                    variant="text"
                    onClick={onReloadWorkspace}
                    disabled={isSending}
                  >
                    Reload chat
                  </Button>
                </Stack>
              }
            >
              Delivery could not be confirmed. You can try the message again.
            </Alert>
          ) : null}

        </Box>

        <Box
          component="form"
          className="chat-form"
          onSubmit={onSubmit}
          sx={{
            flex: 'none',
            bgcolor: M3.surfaceContainerLowest,
            borderTop: '1px solid',
            borderColor: M3.surfaceContainerHighest,
            px: 2,
            pt: 1.25,
            pb: 2.75,
            position: 'relative',
            zIndex: 1,
          }}
        >
          <Stack direction="row" spacing={1.25} sx={{ alignItems: 'center' }}>
            <IconButton
              onClick={onOpenManual}
              aria-label="Open the manual"
              aria-controls="manual-sheet"
              sx={{
                width: TOUCH,
                height: TOUCH,
                bgcolor: M3.surfaceContainerLow,
                fontFamily: MONO,
                fontSize: '0.75rem',
                color: M3.onSurfaceVariant,
                flex: 'none',
              }}
            >
              PDF
            </IconButton>

            {/*
              The 2000-character cap is enforced by the input, which simply
              stops accepting keystrokes. Silent truncation mid-sentence reads
              as a broken keyboard, so the count appears once it is close.
            */}
            {draft.length > 1800 ? (
              <Box
                component="span"
                aria-live="polite"
                sx={{
                  fontFamily: MONO,
                  fontSize: '0.6875rem',
                  color: draft.length >= 2000 ? M3.error : M3.onSurfaceVariant,
                  flex: 'none',
                }}
              >
                {`${draft.length}/2000`}
              </Box>
            ) : null}

            <InputBase
              value={draft}
              onChange={(event) => onDraftChange(event.target.value)}
              placeholder="Ask about this machine"
              disabled={disabled}
              inputProps={{ maxLength: 2000, 'aria-label': 'Message', autoComplete: 'off' }}
              sx={{
                flex: 1,
                minWidth: 0,
                borderRadius: 999,
                bgcolor: M3.surfaceContainerLow,
                border: `1px solid ${M3.outlineVariant}`,
                fontSize: '0.875rem',
                '& .MuiInputBase-input': { height: TOUCH, boxSizing: 'border-box', px: 2 },
              }}
            />

            <IconButton
              type="submit"
              disabled={disabled}
              aria-label={isSending ? 'Sending message' : 'Send'}
              sx={{
                width: TOUCH,
                height: TOUCH,
                flex: 'none',
                bgcolor: M3.primaryContainer,
                color: M3.onPrimary,
                '&:hover': { bgcolor: M3.primary },
                '&.Mui-disabled': { bgcolor: M3.surfaceContainerHigh, color: M3.outline },
              }}
            >
              {isSending ? <CircularProgress size={20} color="inherit" /> : <ArrowUpwardIcon />}
            </IconButton>
          </Stack>
        </Box>
      </Box>
    </Box>
  )
}

function AlarmWalkthroughButton({ disabled, onClick }: { disabled: boolean; onClick: () => void }) {
  const [started, setStarted] = useState(false)
  if (started) return null
  return <Button fullWidth disabled={disabled}
    onClick={() => { setStarted(true); onClick() }}
    sx={{ height: TOUCH, bgcolor: M3.primaryContainer }}>
    Walk me through it
  </Button>
}
