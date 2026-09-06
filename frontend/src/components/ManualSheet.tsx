import AddIcon from '@mui/icons-material/Add'
import ChevronLeftIcon from '@mui/icons-material/ChevronLeft'
import ChevronRightIcon from '@mui/icons-material/ChevronRight'
import CloseIcon from '@mui/icons-material/Close'
import RemoveIcon from '@mui/icons-material/Remove'
import SearchIcon from '@mui/icons-material/Search'
import { useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import Drawer from '@mui/material/Drawer'
import IconButton from '@mui/material/IconButton'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import TextField from '@mui/material/TextField'
import Alert from '@mui/material/Alert'
import { searchManualIndex, describeGatewayError } from '../services/api'
import type { Evidence } from '../types/contracts'

import { M3, MONO, MOTION } from '../theme'
import { PdfPageViewer } from './PdfPageViewer'

interface ManualSheetProps {
  open: boolean
  onClose: () => void
  title: string
  manualUrl: string | null
  documentTitle: string
  page: number | null
  onStepPage: (page: number) => void
  machineId: string
  /**
   * Leading pages before the manual's own page 1.
   *
   * Navigation stays on the PDF index - that is what the renderer takes - but
   * the caption shows the number printed on the sheet, so it agrees with the
   * citation that sent the operator here.
   */
  printedPageOffset?: number
}

/**
 * What the page counter reads.
 *
 * The index is what the renderer uses and what the counter used to show, so a
 * citation reading "page 82" opened a viewer captioned "89 / 155". Front matter
 * has no printed number at all, so it keeps the index and says so.
 */
/**
 * The page a search result names, in the same numbering as the caption.
 *
 * Results listed the PDF index while the reader underneath showed the printed
 * page, so one screen named the same passage "Page 97" and "p. 90".
 */
function searchResultPage(page: number | undefined, offset: number) {
  if (!page) {
    return 'Page'
  }
  if (offset <= 0) {
    return `Page ${page}`
  }

  const printed = page - offset
  return printed < 1 ? 'Front matter' : `Page ${printed}`
}

function manualPageCaption(page: number | null, pageCount: number, offset: number) {
  const index = Math.min(page ?? 1, pageCount)
  if (offset <= 0) {
    return `${index} / ${pageCount}`
  }

  // One numbering, not two. The caption read "p.28 · 35/155": the printed
  // page and the PDF index side by side, which is the same page twice. The
  // printed page is the one the citation names and the paper copy shows, so it
  // is the one kept - counted against the printed pages, not the PDF's.
  const printed = index - offset
  if (printed < 1) {
    return 'Front matter'
  }

  return `p. ${printed} / ${pageCount - offset}`
}

/** The two heights the sheet snaps between, as fractions of the viewport. */
const PEEK = 0.7
const FULL = 0.96

/**
 * The manual, as a bottom sheet over the conversation.
 *
 * The chat stays mounted underneath, so opening a cited page does not unload
 * the answer that cited it - which is the whole reason an operator opens the
 * page in the first place. Drag the grab handle to take it to full height.
 */
export function ManualSheet({
  open,
  onClose,
  title,
  manualUrl,
  documentTitle,
  page,
  onStepPage,
  machineId,
  printedPageOffset = 0,
}: ManualSheetProps) {
  const sourceUrl = manualUrl?.split('#', 1)[0] ?? null
  const [heightFraction, setHeightFraction] = useState(PEEK)
  const [isDragging, setIsDragging] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<Evidence[]>([])
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [searched, setSearched] = useState(false)
  const [zoom, setZoom] = useState(1)
  const searchGeneration = useRef(0)
  const search = async () => {
    if (!query.trim()) return
    const generation = ++searchGeneration.current
    setSearching(true)
    setSearchError(null)
    try {
      const response = await searchManualIndex(machineId, query.trim(), 8)
      if (generation === searchGeneration.current) {
        setResults(response.evidence)
        setSearched(true)
      }
    } catch (error) {
      if (generation === searchGeneration.current) setSearchError(describeGatewayError(error))
    } finally {
      if (generation === searchGeneration.current) setSearching(false)
    }
  }
  const [loadedDocument, setLoadedDocument] = useState<{ sourceUrl: string; pageCount: number } | null>(null)
  const dragRef = useRef<{ startY: number; startFraction: number } | null>(null)
  const pageCount = loadedDocument?.sourceUrl === sourceUrl ? loadedDocument.pageCount : null

  // Closing resets the height, so the sheet does not reappear at whatever
  // height it was dragged to three questions ago.
  const close = () => {
    setHeightFraction(PEEK)
    setSearchOpen(false)
    onClose()
  }

  const onPointerDown = (event: ReactPointerEvent<HTMLElement>) => {
    dragRef.current = { startY: event.clientY, startFraction: heightFraction }
    setIsDragging(true)
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  const onPointerMove = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current
    if (!drag) {
      return
    }

    // Dragging up grows the sheet, so the delta is inverted.
    const delta = (drag.startY - event.clientY) / window.innerHeight
    setHeightFraction(Math.min(FULL, Math.max(0.35, drag.startFraction + delta)))
  }

  const onPointerUp = () => {
    if (!dragRef.current) {
      return
    }

    dragRef.current = null
    setIsDragging(false)

    // Snap to whichever stop is nearer, or dismiss if dragged well below peek.
    if (heightFraction < 0.45) {
      close()
      return
    }

    setHeightFraction(heightFraction > (PEEK + FULL) / 2 ? FULL : PEEK)
  }

  return (
    <Drawer
      anchor="bottom"
      open={open}
      onClose={close}
      id="manual-sheet"
      transitionDuration={MOTION.duration}
      slotProps={{
        transition: { easing: MOTION.easing },
        paper: {
          'aria-label': 'Manual',
          role: 'region',
          sx: {
            height: `${Math.round(heightFraction * 100)}dvh`,
            borderRadius: '20px 20px 0 0',
            boxShadow: '0 -8px 24px rgba(25, 28, 30, .18)',
            transition: isDragging ? 'none' : 'height .2s ease',
            display: 'flex',
            flexDirection: 'column',
          },
        },
      }}
    >
      <Box
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        aria-label="Resize manual"
        sx={{ flex: 'none', py: 1, cursor: 'grab', touchAction: 'none' }}
      >
        <Box sx={{ height: 5, width: 44, bgcolor: M3.outlineVariant, borderRadius: 999, mx: 'auto' }} />
      </Box>

      <Stack
        direction="row"
        spacing={1.5}
        sx={{
          flex: 'none',
          alignItems: 'center',
          px: 2,
          py: 1.25,
          borderBottom: '1px solid',
          borderColor: M3.surfaceContainerHighest,
        }}
      >
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography sx={{ fontFamily: MONO, fontSize: '0.6875rem', lineHeight: '14px', color: M3.outline }}>
            MANUAL
          </Typography>
          <Typography variant="h3" component="h2" noWrap sx={{ fontSize: '1rem', lineHeight: '22px' }}>
            {title}
          </Typography>
        </Box>
        <IconButton
          onClick={() => { setSearchOpen(value => !value); setHeightFraction(FULL) }}
          aria-label="Search the manual"
          sx={{ width: 44, height: 44, bgcolor: M3.surfaceContainerLow }}
        >
          <SearchIcon fontSize="small" />
        </IconButton>
        <IconButton
          onClick={close}
          aria-label="Close"
          sx={{ width: 44, height: 44, bgcolor: M3.surfaceContainerLow }}
        >
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>

      {searchOpen ? <Stack component="form" spacing={1} sx={{ px: 2, py: 1, maxHeight: '45%', overflow: 'auto' }}
        onSubmit={(event) => { event.preventDefault(); void search() }}>
        <Stack direction="row" spacing={1}>
          <TextField autoFocus fullWidth size="small" label="Search this manual" value={query} onChange={event => setQuery(event.target.value)} />
          <Button type="submit" disabled={searching || !query.trim()}>Search</Button>
        </Stack>
        {searching ? <Typography role="status">Searching manual…</Typography> : null}
        {searchError ? <Alert severity="error">{searchError}</Alert> : null}
        {searched && !searching && results.length === 0 ? <Typography role="status">No matching passages. Try describing the component or condition.</Typography> : null}
        {results.filter(item => item.page).map((item, index) => <Button key={item.chunkId ?? index}
          variant="outlined"
          sx={{ textAlign: 'left', display: 'block', flexShrink: 0, borderRadius: 1,
            width: '100%', whiteSpace: 'normal', overflowWrap: 'anywhere',
            px: 1.5, py: 1.25, lineHeight: 1.5, color: M3.onSurface,
            bgcolor: M3.surfaceContainerLowest, borderColor: M3.outlineVariant }}
          onClick={() => { onStepPage(item.page!); setSearchOpen(false) }}>
          {searchResultPage(item.page, printedPageOffset)} — {item.section || item.title}<br />{item.excerpt.slice(0, 220)}
        </Button>)}
      </Stack> : null}
      <Stack direction="row" spacing={0.5} sx={{ px: 1, alignItems: 'center', justifyContent: 'space-between', flex: 'none' }}>
        <IconButton aria-label="Zoom out" disabled={zoom <= 1} onClick={() => setZoom(value => Math.max(1, value - 0.5))}><RemoveIcon fontSize="small" /></IconButton>
        <Button aria-label="Fit page width" onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</Button>
        <IconButton aria-label="Zoom in" disabled={zoom >= 4} onClick={() => setZoom(value => Math.min(4, value + 0.5))}><AddIcon fontSize="small" /></IconButton>
        <Button component="a" href={sourceUrl ?? undefined} target="_blank" rel="noreferrer">Original PDF</Button>
      </Stack>
      <Box sx={{ flex: 1, minHeight: 0, bgcolor: M3.surfaceContainerHigh, p: 1.75 }}>
        {manualUrl ? (
          open ? (
            <PdfPageViewer
              key={sourceUrl}
              url={manualUrl}
              title={documentTitle}
              page={page ?? 1}
              zoom={zoom}
              onDocumentLoad={(count) => {
                setLoadedDocument({ sourceUrl: sourceUrl ?? manualUrl, pageCount: count })
                if (!page) {
                  onStepPage(1)
                }
              }}
            />
          ) : null
        ) : (
          <Box sx={{ display: 'grid', placeItems: 'center', height: '100%' }}>
            <Typography color="text.secondary">
              No manual is available for this machine.
            </Typography>
          </Box>
        )}
      </Box>

      <Stack
        direction="row"
        sx={{
          flex: 'none',
          alignItems: 'center',
          justifyContent: 'space-between',
          px: 2,
          pt: 1.5,
          pb: 2.75,
          borderTop: '1px solid',
          borderColor: M3.surfaceContainerHighest,
        }}
      >
        <ButtonBase
          onClick={() => onStepPage((page ?? 2) - 1)}
          disabled={!page || page <= 1}
          aria-label="Previous page"
          sx={{
            width: 48,
            height: 48,
            borderRadius: 999,
            border: `1px solid ${M3.outline}`,
            color: M3.primary,
            fontSize: '1.125rem',
            '&.Mui-disabled': { opacity: 0.4 },
          }}
        >
          <ChevronLeftIcon />
        </ButtonBase>

        <Typography
          sx={{
            fontFamily: MONO,
            fontSize: '0.75rem',
            fontWeight: 500,
            color: M3.onSurfaceVariant,
            // One line between the two step buttons. Wrapped, it pushed them apart
            // and collided with the sheet's own controls on a narrow phone.
            whiteSpace: 'nowrap',
          }}
        >
          {pageCount ? manualPageCaption(page, pageCount, printedPageOffset) : `p. ${page ?? '—'}`}
        </Typography>

        <ButtonBase
          onClick={() => onStepPage((page ?? 0) + 1)}
          disabled={Boolean(pageCount && (page ?? 1) >= pageCount)}
          aria-label="Next page"
          sx={{
            width: 48,
            height: 48,
            borderRadius: 999,
            border: `1px solid ${M3.outline}`,
            color: M3.primary,
            fontSize: '1.125rem',
            '&.Mui-disabled': { opacity: 0.4 },
          }}
        >
          <ChevronRightIcon />
        </ButtonBase>

        <Button onClick={close} sx={{ height: 48 }}>
          Back to chat
        </Button>
      </Stack>
    </Drawer>
  )
}
