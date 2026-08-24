/**
 * HTTP errors carrying a machine-readable code.
 *
 * The code matters as much as the status: a client must be able to tell "you
 * may not see this" from "there is nothing here" from "the source is down", and
 * a prose message alone cannot be branched on.
 */
export class HttpError extends Error {
  constructor(statusCode, message, options = {}) {
    super(message)
    this.name = 'HttpError'
    this.statusCode = statusCode
    if (options.code) {
      this.code = options.code
    }
    if (options.domain) {
      this.domain = options.domain
    }
    if (options.visibility) {
      this.visibility = options.visibility
    }
  }
}
