import { useEffect, useMemo, useRef, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import {
  GlobalWorkerOptions,
  getDocument,
  TextLayer,
  type PDFDocumentProxy,
} from 'pdfjs-dist'
import './pdf-text-layer.css'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

GlobalWorkerOptions.workerSrc = pdfWorkerUrl

interface PdfPageViewerProps {
  url: string
  title: string
  page: number
  zoom?: number
  onDocumentLoad: (pageCount: number) => void
}

/**
 * A PDF page rendered without the browser's PDF plug-in.
 *
 * Chrome on Android can open a PDF as a top-level document, but it displays a
 * broken-document tile when that same response is placed in an iframe. PDF.js
 * keeps the issued PDF as the source of truth while rendering it to a canvas
 * that works in every supported browser.
 */
export function PdfPageViewer({
  url,
  title,
  page,
  zoom = 1,
  onDocumentLoad,
}: PdfPageViewerProps) {
  const sourceUrl = useMemo(() => url.split('#', 1)[0], [url])
  const containerRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const textRef = useRef<HTMLDivElement | null>(null)
  const onDocumentLoadRef = useRef(onDocumentLoad)
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null)
  const [containerWidth, setContainerWidth] = useState(0)
  const [isLoading, setIsLoading] = useState(false)
  const [isRendering, setIsRendering] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    onDocumentLoadRef.current = onDocumentLoad
  }, [onDocumentLoad])

  useEffect(() => {
    const container = containerRef.current
    if (!container) {
      return
    }

    const updateWidth = () => setContainerWidth(Math.max(0, Math.floor(container.clientWidth)))
    updateWidth()
    const observer = new ResizeObserver(updateWidth)
    observer.observe(container)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!sourceUrl) {
      return
    }

    let disposed = false
    const loadingTask = getDocument({ url: sourceUrl, withCredentials: true })

    void loadingTask.promise
      .then((document) => {
        if (disposed) {
          void loadingTask.destroy()
          return
        }
        setPdf(document)
        setIsLoading(false)
        onDocumentLoadRef.current(document.numPages)
      })
      .catch(() => {
        if (!disposed) {
          setIsLoading(false)
          setError('The manual could not be rendered in the app.')
        }
      })

    return () => {
      disposed = true
      void loadingTask.destroy()
    }
  }, [sourceUrl])

  const renderedPage = pdf ? Math.min(Math.max(1, page), pdf.numPages) : page

  useEffect(() => {
    const canvas = canvasRef.current
    if (!pdf || !canvas || containerWidth <= 0) {
      return
    }

    let disposed = false
    let renderTask: ReturnType<Awaited<ReturnType<typeof pdf.getPage>>['render']> | null = null
    let textLayer: TextLayer | null = null
    setError(null)
    setIsRendering(true)

    void pdf
      .getPage(renderedPage)
      .then((pdfPage) => {
        if (disposed) {
          return null
        }

        const unscaled = pdfPage.getViewport({ scale: 1 })
        const cssWidth = Math.max(1, containerWidth - 24) * zoom
        const pixelRatio = Math.min(window.devicePixelRatio || 1, 2)
        const viewport = pdfPage.getViewport({ scale: (cssWidth / unscaled.width) * pixelRatio })
        canvas.width = Math.floor(viewport.width)
        canvas.height = Math.floor(viewport.height)
        canvas.style.width = `${Math.floor(viewport.width / pixelRatio)}px`
        canvas.style.height = `${Math.floor(viewport.height / pixelRatio)}px`
        renderTask = pdfPage.render({ canvas, viewport })
        if (textRef.current) {
          textRef.current.replaceChildren()
          const textViewport = pdfPage.getViewport({ scale: cssWidth / unscaled.width })
          textRef.current.style.setProperty('--scale-factor', String(textViewport.scale))
          textRef.current.style.setProperty('--total-scale-factor', String(textViewport.scale))
          textLayer = new TextLayer({ container: textRef.current, textContentSource: pdfPage.streamTextContent(), viewport: textViewport })
        }
        return Promise.all([renderTask.promise, textLayer?.render()])
      })
      .then(() => {
        if (!disposed) {
          setIsRendering(false)
        }
      })
      .catch((cause: unknown) => {
        if (!disposed && !(cause instanceof Error && cause.name === 'RenderingCancelledException')) {
          setIsRendering(false)
          setError('This page could not be rendered in the app.')
        }
      })

    return () => {
      disposed = true
      renderTask?.cancel()
      textLayer?.cancel()
    }
  }, [containerWidth, pdf, renderedPage, zoom])

  return (
    <Box
      ref={containerRef}
      sx={{
        width: '100%',
        height: '100%',
        minHeight: 0,
        overflow: 'auto',
        bgcolor: 'grey.200',
        borderRadius: 1,
        position: 'relative',
      }}
    >
      {isLoading ? (
        <Stack
          role="status"
          spacing={1.5}
          sx={{ position: 'absolute', inset: 0, alignItems: 'center', justifyContent: 'center' }}
        >
          <CircularProgress size={28} />
          <Typography color="text.secondary">Loading manual…</Typography>
        </Stack>
      ) : null}

      {error ? (
        <Stack spacing={1.5} sx={{ p: 2, alignItems: 'center', justifyContent: 'center', minHeight: '100%' }}>
          <Alert severity="error">{error}</Alert>
          <Button component="a" href={sourceUrl} target="_blank" rel="noreferrer">
            Open original PDF
          </Button>
        </Stack>
      ) : null}

      {!error ? (
        <Box sx={{ minHeight: '100%', width: 'max-content', minWidth: '100%', p: 1.5, boxSizing: 'border-box' }}>
          <Box sx={{ position: 'relative', width: 'max-content', mx: 'auto' }}>
          <Box
            component="canvas"
            key={`${renderedPage}-${containerWidth}`}
            ref={canvasRef}
            role="img"
            aria-label={`${title}, page ${renderedPage}${pdf ? ` of ${pdf.numPages}` : ''}`}
            sx={{
              display: pdf ? 'block' : 'none',
              bgcolor: 'common.white',
              boxShadow: 2,
              opacity: isRendering ? 0.35 : 1,
            }}
          />
          <div ref={textRef} className="textLayer" aria-label="Manual page text" />
          </Box>
        </Box>
      ) : null}
    </Box>
  )
}

