import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import { useEffect, useState, type ReactNode } from 'react'
import Accordion from '@mui/material/Accordion'
import AccordionDetails from '@mui/material/AccordionDetails'
import AccordionSummary from '@mui/material/AccordionSummary'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import LinearProgress from '@mui/material/LinearProgress'
import Paper from '@mui/material/Paper'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'

import {
  describeGatewayError,
  getMachineOrders,
  getMaintenanceTickets,
  getQuotes,
} from '../services/api'
import type { MaintenanceTicket, OrderRecord, QuoteSummary } from '../types/contracts'

/**
 * Shown in place of a panel the signed-in user may not read.
 *
 * Deliberately visible rather than hidden: an operator should be able to see
 * that the data exists and that their role is what stands in the way, not be
 * left wondering whether the machine simply has none.
 */
export function LockedPanel({ what, visibility }: { what: string; visibility?: string | null }) {
  return (
    <Alert severity="info" variant="outlined" role="note" icon={false}>
      <Typography variant="subtitle2">Restricted</Typography>
      <Typography variant="body2">
        {visibility
          ? `Your ${visibility} role does not include ${what}.`
          : `Your role does not include ${what}.`}
      </Typography>
    </Alert>
  )
}

type ResourceState<T> = { data: T | null; error: string | null }

/**
 * Load a panel's data, keeping the pending, loaded and failed states together.
 *
 * One state object rather than three, so a refetch cannot briefly show the
 * previous machine's data next to the new machine's heading.
 */
function useResource<T>(load: () => Promise<T>, deps: unknown[]) {
  const [state, setState] = useState<ResourceState<T>>({ data: null, error: null })

  useEffect(() => {
    let cancelled = false
    load()
      .then((value) => {
        if (!cancelled) setState({ data: value, error: null })
      })
      .catch((cause) => {
        if (!cancelled) setState({ data: null, error: describeGatewayError(cause) })
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return state
}

function money(value: number | null | undefined, currency: string | null | undefined) {
  if (value === null || value === undefined) return '—'
  return `${value.toLocaleString(undefined, { minimumFractionDigits: 2 })} ${currency ?? ''}`.trim()
}

/**
 * A list from a payload that may not contain one.
 *
 * `?? []` only covers null and undefined: an endpoint answering `{}` where an
 * array was expected still reached `.map`, which threw during render and took
 * the entire workspace - manual and chat included - down to a blank page.
 */
function asList<T>(value: T[] | undefined | null): T[] {
  return Array.isArray(value) ? value : []
}

/** A collapsible data panel with its own heading and a one-line summary. */
function DataPanel({
  title,
  summary,
  children,
}: {
  title: string
  summary: string
  children: ReactNode
}) {
  return (
    <Accordion disableGutters variant="outlined">
      <AccordionSummary
        expandIcon={<ExpandMoreIcon />}
        sx={{ minHeight: 48, '& .MuiAccordionSummary-content': { alignItems: 'center', gap: 1 } }}
      >
        <Typography sx={{ flexGrow: 1 }}>
          {title}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          {summary}
        </Typography>
      </AccordionSummary>
      <AccordionDetails>{children}</AccordionDetails>
    </Accordion>
  )
}

/** Quotations issued to the company, newest first. */
export function CommercialPanel({ machineId }: { machineId: string }) {
  const quotes = useResource<{ quotes: QuoteSummary[] }>(() => getQuotes(), [])
  const orders = useResource<OrderRecord[]>(() => getMachineOrders(machineId), [machineId])

  const quoteList = asList(quotes.data?.quotes)
  const orderList = asList(orders.data)
  const commercialSummary =
    quotes.data === null || orders.data === null
      ? 'Loading records'
      : `${quoteList.length} quote${quoteList.length === 1 ? '' : 's'} / ${orderList.length} order${orderList.length === 1 ? '' : 's'}`

  return (
    <DataPanel title="Quotations" summary={commercialSummary}>
      {quotes.error ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          {quotes.error}
        </Alert>
      ) : null}
      {quotes.data === null && !quotes.error ? <LinearProgress sx={{ mb: 1 }} /> : null}
      {quotes.data !== null && quoteList.length === 0 ? (
        <Typography color="text.secondary" variant="body2">
          No quotations have been issued to your company.
        </Typography>
      ) : null}

      <Stack spacing={1}>
        {quoteList.map((quote) => {
          // The lifecycle lives on the revision; a rejected or expired current
          // revision means the offer is closed, and saying so avoids presenting
          // a dead quotation as if it were live.
          const closed = ['Rejected', 'Expired'].includes(quote.currentRevisionStatus)
          return (
            <Paper
              key={quote.quoteId}
              variant="outlined"
              sx={{ p: 1.5, opacity: closed ? 0.65 : 1 }}
            >
              <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mb: 0.5 }}>
                <Typography variant="subtitle2" sx={{ flexGrow: 1 }}>
                  {quote.quoteId}
                </Typography>
                <Chip
                  size="small"
                  label={quote.currentRevisionStatus}
                  color={closed ? 'default' : 'primary'}
                  variant={closed ? 'outlined' : 'filled'}
                />
              </Stack>
              <Typography variant="body2">{quote.description}</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                Revision {quote.currentRevisionNumber} of {quote.revisionCount} ·{' '}
                {money(quote.currentTotal, quote.currency)}
                {quote.currentDiscountRate
                  ? ` · ${Math.round(quote.currentDiscountRate * 100)}% discount already applied`
                  : ''}
              </Typography>
              {quote.currentChangeSummary ? (
                <Typography variant="caption" sx={{ display: 'block', mt: 0.5 }}>
                  {quote.currentChangeSummary}
                </Typography>
              ) : null}
            </Paper>
          )
        })}
      </Stack>

      <Typography variant="h3" sx={{ mt: 2, mb: 1 }}>
        Orders for this machine
      </Typography>
      {orders.error ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          {orders.error}
        </Alert>
      ) : null}
      {orders.data !== null && orderList.length === 0 ? (
        <Typography color="text.secondary" variant="body2">
          No orders reference this machine.
        </Typography>
      ) : null}
      <Stack spacing={1}>
        {orderList.map((order) => (
          <Paper key={order.orderId} variant="outlined" sx={{ p: 1.5 }}>
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mb: 0.5 }}>
              <Typography variant="subtitle2" sx={{ flexGrow: 1 }}>
                {order.orderId}
              </Typography>
              <Chip size="small" variant="outlined" label={order.orderStatus} />
            </Stack>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
              Ordered {order.orderDate} · {money(order.total, order.currency)} ·{' '}
              {order.shipmentStatus}
            </Typography>
            {order.orderedAfterQuoteExpiry ? (
              <Alert severity="warning" sx={{ mt: 1 }}>
                Placed after the quotation&rsquo;s validity date had passed.
              </Alert>
            ) : null}
            <Box component="ul" sx={{ pl: 2, mt: 1, mb: 0 }}>
              {asList(order.lines).map((line) => (
                <Typography component="li" variant="body2" key={line.quoteLineId}>
                  {line.description} — {money(line.price, order.currency)}
                </Typography>
              ))}
            </Box>
          </Paper>
        ))}
      </Stack>
    </DataPanel>
  )
}

/** Service and maintenance activity recorded against this machine. */
export function MaintenancePanel({ machineId }: { machineId: string }) {
  const { data, error } = useResource<{ tickets: MaintenanceTicket[] }>(
    () => getMaintenanceTickets(machineId),
    [machineId],
  )

  const tickets = asList(data?.tickets)
  const open = tickets.filter((ticket) =>
    ['Open', 'In progress', 'Waiting for parts'].includes(ticket.ticketStatus),
  )
  const maintenanceSummary =
    data === null
      ? 'Loading records'
      : `${tickets.length} ticket${tickets.length === 1 ? '' : 's'}${open.length ? ` / ${open.length} open` : ''}`

  return (
    <DataPanel title="Maintenance" summary={maintenanceSummary}>
      {error ? (
        <Alert severity="error" sx={{ mb: 1 }}>
          {error}
        </Alert>
      ) : null}
      {data === null && !error ? <LinearProgress sx={{ mb: 1 }} /> : null}
      {data !== null && tickets.length === 0 ? (
        <Typography color="text.secondary" variant="body2">
          No maintenance has been recorded for this machine.
        </Typography>
      ) : null}
      {open.length > 0 ? (
        <Typography variant="body2" sx={{ mb: 1 }}>
          {open.length} ticket{open.length === 1 ? '' : 's'} still open.
        </Typography>
      ) : null}

      <Stack spacing={1}>
        {tickets.map((ticket) => (
          <Paper
            key={ticket.ticketId}
            variant="outlined"
            className={`ticket ticket--${ticket.priority.toLowerCase()}`}
            sx={{ p: 1.5, minWidth: 0 }}
          >
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center', mb: 0.5 }}>
              <Typography variant="subtitle2" sx={{ flexGrow: 1, minWidth: 0 }}>
                {ticket.ticketId}
              </Typography>
              <Chip
                size="small"
                variant="outlined"
                label={ticket.ticketStatus}
                sx={{ flexShrink: 0 }}
              />
            </Stack>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
              {ticket.ticketType} · {ticket.priority} priority · {ticket.createdDate} ·{' '}
              {ticket.ownerRole}
            </Typography>
            {/* A ticket may have no originating alarm, which is a real state in
                the data rather than missing information. */}
            <Typography
              variant="caption"
              sx={{ display: 'block', mt: 0.5, overflowWrap: 'anywhere', wordBreak: 'break-word' }}
              color="text.secondary"
            >
              {ticket.alarmCode
                ? `Raised by ${ticket.alarmCode} (${ticket.alarmSeverity})`
                : 'Not raised by an alarm'}
            </Typography>
          </Paper>
        ))}
      </Stack>
    </DataPanel>
  )
}
