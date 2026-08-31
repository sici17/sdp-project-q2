import type { Evidence } from './types/contracts'


const boilerplateLines = [
  /^AROL S\.p\.A\.\s*-\s*teaching copy, Politecnico di Torino, System and Device Programming\. Do not redistribute\.?$/i,
  /^COPYRIGHT BY CLOSYS S\.r\.l\. NO REPRODUCTION OR CHANGE.*ALLOWED$/i,
  /^-+\s*Blank page\s*-+$/i,
]
const nonOperationalLabels = [
  'notice educational use only',
  'report of the training',
  'training report',
]
const trainingFormMarkers = [
  'trainer/s signature',
  "trainer's signature",
  'name charge signature',
]
const dotLeader = /(?:\.\s*){12,}/

function cleanManualExcerpt(value: string) {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => !boilerplateLines.some((pattern) => pattern.test(line)))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

function sanitizeManualEvidence(item: Evidence): Evidence | null {
  const label = `${item.title ?? ''} ${item.section ?? ''}`.toLowerCase()
  if (nonOperationalLabels.some((marker) => label.includes(marker))) {
    return null
  }

  const excerpt = cleanManualExcerpt(item.excerpt ?? '')
  const normalized = excerpt.replace(/\s+/g, ' ').trim().toLowerCase()
  if (!normalized || normalized.includes('no operational reliance')) {
    return null
  }
  if (trainingFormMarkers.some((marker) => normalized.includes(marker))) {
    return null
  }
  if (dotLeader.test(excerpt)) {
    return null
  }

  const words = normalized.match(/[a-z0-9]+/g) ?? []
  if (words.length < 3 || normalized.length < 16) {
    return null
  }

  return { ...item, excerpt }
}

export function sanitizeEvidence(items: Evidence[]) {
  return items.flatMap((item) => {
    if (!item || typeof item !== 'object') {
      return []
    }
    if (item.source !== 'manual') {
      return [item]
    }

    const sanitized = sanitizeManualEvidence(item)
    return sanitized ? [sanitized] : []
  })
}
