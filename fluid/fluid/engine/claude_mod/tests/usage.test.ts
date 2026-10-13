// Offline checks for the mod's usage reports: `claude plugin test <this mod>`.
// The test's hooks stand for Claude Code and the host beneath the mod: the
// environment DelegatedLoop sets, the clock, and the bridge `$.http.fetch`
// reaches.
import { describe, expect, mock, test } from 'claude-code/testing'
import { REFRESH_MS, bridgeFrom, shouldPost, usageReport } from '../hooks/usage.ts'

const LIMITS = [
  { kind: 'five_hour', percentUsed: 23.5, resetsAt: '2026-10-03T18:00:00Z' },
  { kind: 'seven_day', percentUsed: 7 },
]
const CONTEXT = { tokens: 1200, window: 200000, percent: 1 }
const URL = 'http://127.0.0.1:50123/claude-mod/usage'
const ENV = { FLUID_CLAUDE_MOD_URL: URL, FLUID_CLAUDE_MOD_TOKEN: 'session-token' }
const START = { cwd: '/work', surface: null, isInteractive: false }

/** Claude Code's own answers to the two session events, beneath the mod. */
function engine(on: Parameters<Parameters<typeof test>[1]>[1]): void {
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.measure', (_$, e) => ({ changed: e.changed }))
}

describe('usageReport', () => {
  test('carries the kind, the share used and the reset time, nothing else', () => {
    const withExtra = [{ ...LIMITS[0], secret: 'x' } as (typeof LIMITS)[0]]
    expect(JSON.parse(usageReport(withExtra, 1000) ?? 'null')).toEqual({
      v: 1, measuredAt: 1000,
      rateLimits: [{ kind: 'five_hour', percentUsed: 23.5, resetsAt: '2026-10-03T18:00:00Z' }],
    })
  })

  test('leaves the reset out when the CLI gave none', () => {
    expect(JSON.parse(usageReport([LIMITS[1]], 5) ?? 'null').rateLimits).toEqual([
      { kind: 'seven_day', percentUsed: 7 },
    ])
  })

  test('has nothing to report off a subscription', () => {
    expect(usageReport([], 1000)).toBe(null)
    expect(usageReport([{ kind: 'five_hour', percentUsed: Number.NaN }], 1000)).toBe(null)
  })
})

describe('bridgeFrom', () => {
  test('takes a loopback port or a unix socket', () => {
    expect(bridgeFrom(URL, 't')).toEqual({ url: URL, token: 't' })
    expect(bridgeFrom('http://localhost/claude-mod/usage', 't', '/tmp/s/bundled.sock')).toEqual({
      url: 'http://localhost/claude-mod/usage', token: 't', socketPath: '/tmp/s/bundled.sock',
    })
  })

  test('refuses anything else, or a missing value', () => {
    expect(bridgeFrom(undefined, 't')).toBe(null)
    expect(bridgeFrom(URL, undefined)).toBe(null)
    expect(bridgeFrom('https://example.com/claude-mod/usage', 't')).toBe(null)
    expect(bridgeFrom('http://10.0.0.5:80/claude-mod/usage', 't')).toBe(null)
    expect(bridgeFrom('http://localhost/claude-mod/usage', 't')).toBe(null)
  })
})

describe('shouldPost', () => {
  test('posts a moved window at once and an unchanged one once a minute', () => {
    expect(shouldPost(['context'], 0, null)).toBe(true)
    expect(shouldPost(['rateLimits'], 10, 5)).toBe(true)
    expect(shouldPost(['context'], 10, 5)).toBe(false)
    expect(shouldPost(['context'], 5 + REFRESH_MS, 5)).toBe(true)
  })
})

describe('through the hooks', () => {
  test('a measurement is posted to the bridge with the token, and the env is cleared', async ($, on) => {
    engine(on)
    const clock = mock.clock(on, { now: 1_000_000 })
    mock.env(on, ENV)
    const cleared: string[] = []
    on('env.set', (_$, e) => {
      cleared.push(e.name)
      return { value: undefined }
    })
    const posts: { url: string; init: unknown }[] = []
    on('http.fetch', (_$, e) => {
      posts.push({ url: e.url, init: e.init })
      return { status: 204, ok: true, headers: {}, text: '' }
    })
    await $.session.start(START)
    expect(cleared.sort()).toEqual(['FLUID_CLAUDE_MOD_TOKEN', 'FLUID_CLAUDE_MOD_URL'])
    await $.session.measure({ context: CONTEXT, rateLimits: LIMITS, changed: ['context', 'rateLimits'] })
    await clock.settle()
    expect(posts.length).toBe(1)
    expect(posts[0].url).toBe(URL)
    const init = posts[0].init as { method: string; headers: Record<string, string>; body: string }
    expect(init.method).toBe('POST')
    expect(init.headers.authorization).toBe('Bearer session-token')
    expect(JSON.parse(init.body).measuredAt).toBe(1_000_000)
  })

  test('an unchanged reading waits a minute; a moved window goes at once', async ($, on) => {
    engine(on)
    const clock = mock.clock(on, { now: 0 })
    mock.env(on, ENV)
    on('env.set', () => ({ value: undefined }))
    let posts = 0
    on('http.fetch', () => {
      posts += 1
      return { status: 204, ok: true, headers: {}, text: '' }
    })
    await $.session.start(START)
    const measure = (changed: ('context' | 'rateLimits')[]) =>
      $.session.measure({ context: CONTEXT, rateLimits: LIMITS, changed })
    await measure(['context', 'rateLimits'])
    await clock.settle()
    await clock.advance(1000)
    await measure(['context'])
    await clock.settle()
    expect(posts).toBe(1)
    await measure(['rateLimits'])
    await clock.settle()
    expect(posts).toBe(2)
    await clock.advance(REFRESH_MS)
    await measure(['context'])
    await clock.settle()
    expect(posts).toBe(3)
  })

  test('nothing is posted without a bridge or without windows', async ($, on) => {
    engine(on)
    mock.clock(on, { now: 0 })
    mock.env(on, {})
    let posts = 0
    on('http.fetch', () => {
      posts += 1
      return { status: 204, ok: true, headers: {}, text: '' }
    })
    await $.session.start(START)
    await $.session.measure({ context: CONTEXT, rateLimits: LIMITS, changed: ['rateLimits'] })
    expect(posts).toBe(0)
  })

  test('a failed post never fails the measurement', async ($, on) => {
    engine(on)
    const clock = mock.clock(on, { now: 0 })
    mock.env(on, ENV)
    on('env.set', () => ({ value: undefined }))
    on('http.fetch', () => {
      throw new Error('connection refused')
    })
    await $.session.start(START)
    const answer = await $.session.measure({ context: CONTEXT, rateLimits: LIMITS, changed: ['rateLimits'] })
    await clock.settle()
    expect(answer.changed).toEqual(['rateLimits'])
  })

  test('off a subscription the measurement passes through untouched', async ($, on) => {
    engine(on)
    mock.clock(on, { now: 0 })
    mock.env(on, ENV)
    on('env.set', () => ({ value: undefined }))
    let posts = 0
    on('http.fetch', () => {
      posts += 1
      return { status: 204, ok: true, headers: {}, text: '' }
    })
    await $.session.start(START)
    const answer = await $.session.measure({ context: CONTEXT, rateLimits: [], changed: ['context'] })
    expect(answer.changed).toEqual(['context'])
    expect(posts).toBe(0)
  })
})
