/** One fetch wrapper for the whole app: JSON in/out, the CSRF header on writes, and one error type with the server's code and message. */
export class ApiError extends Error {
  status: number
  code: string
  body: Record<string, unknown>
  constructor(status: number, code: string, message: string, body: Record<string, unknown> = {}) {
    super(message)
    this.status = status
    this.code = code
    this.body = body
  }
}

export function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)algo_csrf=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : ''
}

type Opts = { method?: string; json?: unknown; signal?: AbortSignal }

export async function api<T = unknown>(url: string, opts: Opts = {}): Promise<T> {
  const method = (opts.method ?? (opts.json !== undefined ? 'POST' : 'GET')).toUpperCase()
  const headers: Record<string, string> = {}
  if (opts.json !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET' && method !== 'HEAD') headers['X-CSRF-Token'] = csrfToken()
  let res: Response
  try {
    res = await fetch(url, { method, headers, body: opts.json !== undefined ? JSON.stringify(opts.json) : undefined, signal: opts.signal, credentials: 'same-origin' })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e
    throw new ApiError(0, 'NETWORK', 'Could not reach the server.')
  }
  const text = await res.text()
  let body: Record<string, unknown> = {}
  try {
    body = text ? JSON.parse(text) : {}
  } catch {
    /* not JSON (an HTML error page): keep the empty body */
  }
  if (!res.ok) {
    const detail = typeof body.detail === 'string' ? body.detail : undefined
    const err = new ApiError(res.status, String(body.error ?? res.status), String(body.message ?? detail ?? `HTTP ${res.status}`), body)
    if (res.status === 401) window.dispatchEvent(new CustomEvent('auth:required', { detail: err }))
    throw err
  }
  return body as T
}

export const get = <T,>(url: string, signal?: AbortSignal) => api<T>(url, { signal })
export const post = <T,>(url: string, json: unknown = {}) => api<T>(url, { method: 'POST', json })
export const put = <T,>(url: string, json: unknown = {}) => api<T>(url, { method: 'PUT', json })
export const del = <T,>(url: string) => api<T>(url, { method: 'DELETE' })
