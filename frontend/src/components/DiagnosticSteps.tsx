import { useState } from 'react'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'

import type { DiagnosticStep } from '../types/contracts'

/** How much of a step's detail is worth showing before asking. */
const DETAIL_PREVIEW_CHARS = 200

/**
 * One troubleshooting step.
 *
 * `detail` can be a whole retrieved manual passage - subsection numbers, table
 * rows and all - which turned a five-step checklist into a wall of text on a
 * phone. It is collapsed here rather than truncated in the payload, because the
 * operator following the step is the one who should decide they need the rest.
 */
export function DiagnosticStepCard({
  step,
  isChecked,
  disabled,
  onDone,
  onStillFailing,
}: {
  step: DiagnosticStep
  isChecked: boolean
  disabled: boolean
  onDone: () => void
  onStillFailing: () => void
}) {
  const [isExpanded, setIsExpanded] = useState(false)
  const detail = step.detail ?? ''
  const isLong = detail.length > DETAIL_PREVIEW_CHARS
  const shown = isLong && !isExpanded ? `${detail.slice(0, DETAIL_PREVIEW_CHARS).trimEnd()}...` : detail

  return (
    <Paper component="li" variant="outlined" sx={{ p: 1.5, opacity: isChecked ? 0.6 : 1 }}>
      <Typography variant="subtitle2">{step.label}</Typography>
      <Typography variant="body2" color="text.secondary">
        {shown}
      </Typography>
      {isLong ? (
        <Button
          size="small"
          onClick={() => setIsExpanded((current) => !current)}
          sx={{ px: 0.5, minWidth: 0 }}
        >
          {isExpanded ? 'Show less' : 'Show more'}
        </Button>
      ) : null}
      {step.expectedOutcome ? (
        <Typography variant="body2" sx={{ mt: 0.5, fontStyle: 'italic' }}>
          {step.expectedOutcome}
        </Typography>
      ) : null}
      {/*
        This line used to read "troubleshooting / page 47 / safety-critical /
        operator": internal field names joined with slashes. What an operator
        needs from it is where to look it up and whether they are allowed to do
        it themselves.
      */}
      <Stack direction="row" spacing={0.5} sx={{ mt: 1, flexWrap: 'wrap', gap: 0.5 }}>
        {step.page ? (
          <Chip size="small" variant="outlined" label={`Manual page ${step.printedPage ?? step.page}`} />
        ) : null}
        {step.safetyLevel === 'safety-critical' ? (
          <Chip size="small" color="error" variant="outlined" label="Safety-critical" />
        ) : null}
        {step.requiresTechnician ? (
          <Chip size="small" color="warning" variant="outlined" label="Technician required" />
        ) : null}
      </Stack>
      <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
        <Button
          size="small"
          variant={isChecked ? 'contained' : 'text'}
          onClick={onDone}
          aria-pressed={isChecked}
        >
          {isChecked ? 'Done' : 'Mark done'}
        </Button>
        <Button
          size="small"
          variant="outlined"
          color="warning"
          onClick={onStillFailing}
          disabled={disabled}
        >
          Still failing
        </Button>
      </Stack>
    </Paper>
  )
}


/**
 * The troubleshooting checklist, shared by both layouts.
 *
 * It lived inside the desktop chat pane, so the phone - which renders its own
 * workspace rather than reusing that pane - showed no steps at all. An operator
 * on the plant floor is the one holding a phone, so that was backwards.
 */
export function DiagnosticStepList({
  steps,
  checks,
  stepKey,
  disabled,
  onFollowUp,
}: {
  steps: DiagnosticStep[]
  checks: Record<string, boolean>
  stepKey: (step: DiagnosticStep, index: number) => string
  disabled: boolean
  onFollowUp: (step: DiagnosticStep, index: number, outcome: 'completed' | 'failed') => void
}) {
  if (steps.length === 0) {
    return <Typography color="text.secondary">No troubleshooting steps yet.</Typography>
  }

  return (
    <Stack component="ol" spacing={2} sx={{ listStyle: 'none', p: 0, m: 0 }}>
      {steps.map((step, index) => {
        const key = stepKey(step, index)
        return (
          <DiagnosticStepCard
            key={key}
            step={step}
            isChecked={Boolean(checks[key])}
            disabled={disabled}
            onDone={() => onFollowUp(step, index, 'completed')}
            onStillFailing={() => onFollowUp(step, index, 'failed')}
          />
        )
      })}
    </Stack>
  )
}
