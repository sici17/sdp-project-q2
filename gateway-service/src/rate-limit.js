export class RateLimitExceeded extends Error {
  constructor(message = 'Rate limit exceeded.') {
    super(message)
    this.statusCode = 429
  }
}

// The gateway runs as a single process, so its counters live in that process.
// A shared Redis counter only buys something across replicas, and there are
// none: Compose and the local workflow both run exactly one gateway.
export function createRateLimiter({ max, windowMs }) {
  if (max <= 0 || windowMs <= 0) {
    return new DisabledRateLimiter()
  }

  return new MemoryRateLimiter({ max, windowMs })
}

class DisabledRateLimiter {
  async enforce() {}
}

class MemoryRateLimiter {
  constructor({ max, windowMs }) {
    this.max = max
    this.windowMs = windowMs
    this.buckets = new Map()
  }

  async enforce(key) {
    const now = Date.now()
    const bucket = this.buckets.get(key)

    if (!bucket || bucket.resetAt <= now) {
      this.buckets.set(key, { count: 1, resetAt: now + this.windowMs })
      return
    }

    bucket.count += 1

    if (bucket.count > this.max) {
      throw new RateLimitExceeded()
    }
  }
}
