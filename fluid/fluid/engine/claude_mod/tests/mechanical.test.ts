import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'
import { compactionBridge, mounted } from '../hooks/mechanical.ts'

const START = { cwd: '/work', surface: null, isInteractive: false }
const ROWS = [
  { role: 'user' as const, text: 'Original task', toolUses: [], handle: 'user-1' },
  { role: 'assistant' as const, text: 'Working', toolUses: [], handle: 'assistant-1' },
]
const BUILT = [{ role: 'user' as const, text: 'Recovered through HistoryExpand("node_abc")', toolUses: [] }]
const ENV = { FLUID_CLAUDE_MOD_URL: 'http://127.0.0.1:50123/claude-mod/usage',
  FLUID_CLAUDE_MOD_TOKEN: 'mod-token', FLUID_CLAUDE_MOD_COMPACT_URL: 'http://127.0.0.1:50123/claude-mod/compact' }
function engine(on: On, log: () => void = () => {}) {
  const calls: unknown[] = []
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.compact', (_$, e) => { calls.push(e); return { messages: ROWS } })
  on('env.set', () => ({ value: undefined }))
  on('ui.log', () => { log(); return { value: undefined } })
  return calls
}

describe('mechanical compaction', () => {
  test('pins the compaction path for both transports, not another authorized use', () => {
    expect(compactionBridge({ url: ENV.FLUID_CLAUDE_MOD_URL, token: 't' })).toEqual({
      url: 'http://127.0.0.1:50123/claude-mod/compact', token: 't',
    })
    expect(compactionBridge({ url: 'http://localhost/claude-mod/usage', token: 't', socketPath: '/tmp/b.sock' })).toEqual({
      url: 'http://localhost/claude-mod/compact', token: 't', socketPath: '/tmp/b.sock',
    })
    expect(compactionBridge({ url: 'http://127.0.0.1:50123/other', token: 't' })).toBe(null)
    expect(compactionBridge({ url: 'https://example.com/claude-mod/usage', token: 't' })).toBe(null)
  })

  test('only this bridge\'s own pinned route counts as mounted', () => {
    const tcp = { url: ENV.FLUID_CLAUDE_MOD_URL, token: 't' }
    const unix = { url: 'http://localhost/claude-mod/usage', token: 't', socketPath: '/tmp/b.sock' }
    expect(mounted(tcp, ENV.FLUID_CLAUDE_MOD_COMPACT_URL)).toBe(true)
    expect(mounted(unix, 'http://localhost/claude-mod/compact')).toBe(true)
    expect(mounted(tcp, undefined)).toBe(false)
    expect(mounted(tcp, '')).toBe(false)
    expect(mounted(tcp, 'http://127.0.0.1:50124/claude-mod/compact')).toBe(false)
    expect(mounted(tcp, 'http://localhost/claude-mod/compact')).toBe(false)
    expect(mounted(null, ENV.FLUID_CLAUDE_MOD_COMPACT_URL)).toBe(false)
  })

  test('the bridge is captured before the engine can compact during session.start', async ($, on) => {
    const cleared: string[] = []
    let readyAtStart = false
    on('session.start', (_$, e) => {
      readyAtStart = cleared.includes('FLUID_CLAUDE_MOD_TOKEN') && cleared.includes('FLUID_CLAUDE_MOD_COMPACT_URL')
      return { cwd: e.cwd }
    })
    on('env.set', (_$, e) => { cleared.push(e.name); return { value: undefined } })
    mock.env(on, ENV)
    await $.session.start(START)
    expect(readyAtStart).toBe(true)
  })

  test('an oversized resume can compact before session.start', async ($, on) => {
    const calls = engine(on)
    mock.env(on, ENV)
    mock.clock(on, { now: 0 })
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ messages: BUILT }) } }))
    expect(await $.session.compact({ trigger: 'manual', messages: ROWS })).toEqual({ messages: BUILT })
    await $.session.start(START)
    expect(await $.session.compact({ trigger: 'auto', messages: ROWS })).toEqual({ messages: BUILT })
    expect(calls.length).toBe(0)
  })

  test('posts the exact rows and handles, and bypasses the summarizer', async ($, on) => {
    const calls = engine(on)
    mock.env(on, ENV)
    mock.clock(on, { now: 0 })
    let posted: unknown
    on('http.fetch', (_$, e) => {
      posted = e
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ messages: BUILT }) } }
    })
    await $.session.start(START)
    expect(await $.session.compact({ trigger: 'auto', messages: ROWS })).toEqual({ messages: BUILT })
    expect(calls.length).toBe(0)
    const post = posted as { url: string; init: { body: string; headers: Record<string, string> } }
    expect(post.url).toBe('http://127.0.0.1:50123/claude-mod/compact')
    expect(post.init.headers.authorization).toBe('Bearer mod-token')
    expect(JSON.parse(post.init.body)).toEqual({ v: 1, messages: ROWS })
  })

  for (const response of [
    { status: 500, ok: false, headers: {}, text: '' },
    { status: 200, ok: true, headers: {}, text: 'bad json' },
    { status: 200, ok: true, headers: {}, text: '{"messages":[]}' },
    { status: 200, ok: true, headers: {}, text: JSON.stringify({ messages: [{ ...ROWS[0], handle: 'foreign' }] }) },
    { status: 200, ok: true, headers: {}, text: JSON.stringify({ messages: [{ ...ROWS[0], text: 'changed' }] }) },
    { status: 200, ok: true, headers: {}, text: JSON.stringify({ messages: [{ role: 'user', text: '', toolUses: [], toolResults: [{ tool_use_id: 'orphan', text: '' }] }] }) },
  ]) {
    test(`falls back unchanged on a rejected response ${response.text}`, async ($, on) => {
      let logs = 0
      const calls = engine(on, () => { logs++ })
      mock.env(on, ENV)
      mock.clock(on, { now: 0 })
      on('http.fetch', () => ({ value: response }))
      await $.session.start(START)
      const event = { trigger: 'auto' as const, messages: ROWS }
      expect(await $.session.compact(event)).toEqual({ messages: ROWS })
      expect(calls).toEqual([event])
      expect(logs).toBe(1)
    })
  }

  for (const route of [undefined, '', 'http://127.0.0.1:50124/claude-mod/compact', 'http://127.0.0.1:50123/claude-mod/usage', '1']) {
    test(`no mounted route (${route}) preserves the old note-only bytes`, async ($, on) => {
      const calls = engine(on)
      const { FLUID_CLAUDE_MOD_COMPACT_URL: _mounted, ...usageOnly } = ENV
      mock.env(on, route === undefined ? usageOnly : { ...usageOnly, FLUID_CLAUDE_MOD_COMPACT_URL: route })
      let posts = 0
      on('http.fetch', () => { posts++; return { deny: 'unexpected post' } })
      await $.session.start(START)
      await $.session.compact({ trigger: 'manual', instructions: 'Keep the task.\n', messages: ROWS })
      const event = calls[0] as { instructions: string; messages: unknown }
      expect(event.instructions).toBe('Keep the task.\n\nAI Shuffle puts its own reminders back after this summary: saved memory, the environment note, Away mode and the session ledger. Do not restate them. Quote the user\'s most recent request word for word, and keep open questions, decisions and promised follow-ups.')
      expect(event.messages).toEqual(ROWS)
      expect(posts).toBe(0)
    })
  }

  test('precompute does not start Claude Code summarization', async ($, on) => {
    const calls = engine(on)
    mock.env(on, ENV)
    await $.session.start(START)
    expect(await $.session.compact({ trigger: 'precompute', messages: ROWS })).toEqual({
      skip: 'Fluid compacts mechanically at the threshold',
    })
    expect(calls.length).toBe(0)
  })

  test('a hung bridge times out and logs once', async ($, on) => {
    let logs = 0
    const calls = engine(on, () => { logs++ })
    mock.env(on, ENV)
    const clock = mock.clock(on, { now: 0 })
    on('http.fetch', () => new Promise(() => {}))
    await $.session.start(START)
    const event = { trigger: 'auto' as const, messages: ROWS }
    const pending = $.session.compact(event)
    await clock.settle()
    await clock.advance(10_000)
    expect(await pending).toEqual({ messages: ROWS })
    expect(calls).toEqual([event])
    expect(logs).toBe(1)
  })

  test('subagent compactions do not post, even when on', async ($, on) => {
    const calls = engine(on)
    mock.env(on, ENV)
    let posts = 0
    on('http.fetch', () => { posts++; throw new Error('unexpected') })
    await $.session.start(START)
    await $.session.compact({ trigger: 'auto', agentId: 'subagent', messages: ROWS })
    expect(posts).toBe(0)
    expect(calls.length).toBe(1)
  })

  test('a transport failure logs once and calls the unchanged next event', async ($, on) => {
    const calls = engine(on)
    mock.env(on, ENV)
    mock.clock(on, { now: 0 })
    on('http.fetch', () => { throw new Error('connection refused') })
    await $.session.start(START)
    const event = { trigger: 'manual' as const, instructions: 'Keep this', messages: ROWS }
    await $.session.compact(event)
    expect(calls).toEqual([event])
  })
})
