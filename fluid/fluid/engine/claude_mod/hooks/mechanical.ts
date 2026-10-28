// The compaction bridge response is untrusted until its handles, roles and tool
// pairs validate. A rejected response never changes the event passed to next.
import type { UsageBridge } from './usage.ts'

type Row = {
  role: 'user' | 'assistant'; text: string; handle?: string
  toolUses: { tool_use_id: string; tool: string; input: Record<string, unknown> }[]
  toolResults?: { tool_use_id: string; text: string; isError?: boolean }[]
}

/**
 * The rows as the bridge is sent them: the fields Fluid matches and rebuilds
 * from, nothing else. The engine also lists each tool outcome under its call
 * (`result`, `text`) and again as `result` beside its tool_result: four copies
 * of every output, and an image-heavy session's post then failed in the
 * engine's own fetch before it reached the bridge. A row handed back with its
 * handle stands as the engine has it, so none of that has to travel.
 */
export function postedRows(messages: readonly Row[]): Row[] {
  return messages.map((m) => ({
    role: m.role,
    text: m.text,
    toolUses: m.toolUses.map((u) => ({ tool_use_id: u.tool_use_id, tool: u.tool, input: u.input })),
    ...(m.toolResults === undefined ? {} : {
      toolResults: m.toolResults.map((r) => ({
        tool_use_id: r.tool_use_id, text: r.text, ...(r.isError === undefined ? {} : { isError: r.isError }),
      })),
    }),
    ...(m.handle === undefined ? {} : { handle: m.handle }),
  }))
}

/** How long the hook waits for Fluid's answer: it compacts the whole stored
 * conversation, which can take longer than a usage post may. */
export const COMPACT_TIMEOUT_MS = 60_000

/** The bridge's answer: rows that stand in the conversation's place, or a
 * skip (Fluid has nothing more to reclaim) with the reason Claude Code prints
 * after "Not compacted · ". Anything else is refused. */
export function compactionAnswer(
  body: unknown, originals: readonly Row[],
): { messages: Row[] } | { skip: string } | { fallback: string } {
  for (const key of ['skip', 'fallback'] as const) {
    if (body && typeof body === 'object' && key in body) {
      // `fallback`: the host's emergency compaction at the context limit, where
      // Fluid freed nothing; Claude Code's summarizer may stand in.
      const reason = (body as Record<string, unknown>)[key]
      if ('messages' in body || ('skip' in body && 'fallback' in body) ||
          typeof reason !== 'string' || reason.length === 0 || reason.length > 500) {
        throw new Error('validation')
      }
      return key === 'skip' ? { skip: reason } : { fallback: reason }
    }
  }
  return { messages: compactedRows(body, originals) }
}

export function compactedRows(body: unknown, originals: readonly Row[]): Row[] {
  if (!body || typeof body !== 'object' || !('messages' in body)) throw new Error('validation')
  const rows = body.messages
  if (!Array.isArray(rows) || rows.length === 0 || rows.length > 20_000) throw new Error('validation')
  const handles = new Map(originals.map((m) => [m.handle, JSON.stringify(m)]))
  if (rows[0]?.role !== 'user') throw new Error('validation')
  const originalCalls = new Map(originals.flatMap((m) => m.toolUses.map((u) => [u.tool_use_id, u.tool] as const)))
  const seen = new Set<string>()
  const calls = new Set<string>()
  const pending = new Set<string>()
  let previous: string | undefined
  for (const row of rows) {
    if (!row || (row.role !== 'user' && row.role !== 'assistant') ||
        typeof row.text !== 'string' || !Array.isArray(row.toolUses) ||
        (row.toolResults !== undefined && !Array.isArray(row.toolResults))) throw new Error('validation')
    // Roles alternate. An open tool group (a parallel group kept as the
    // engine's own rows) may continue its calls over consecutive assistant
    // rows, then its results over consecutive user rows; nothing else comes
    // between a call and its result.
    if (pending.size) {
      const moreCalls = row.role === 'assistant' && previous === 'assistant' && row.toolUses.length > 0
      const moreResults = row.role === 'user' && (row.toolResults?.length ?? 0) > 0
      if (!moreCalls && !moreResults) throw new Error('validation')
    } else if (row.role === previous) {
      throw new Error('validation')
    }
    previous = row.role
    if (row.handle !== undefined) {
      if (typeof row.handle !== 'string' || seen.has(row.handle) ||
          handles.get(row.handle) !== JSON.stringify(row)) throw new Error('validation')
      seen.add(row.handle)
    }
    const results = row.toolResults ?? []
    if ((row.toolUses.length && row.role !== 'assistant') || (results.length && row.role !== 'user')) {
      throw new Error('validation')
    }
    for (const result of results) {
      if (!result || typeof result.text !== 'string' || !pending.delete(result.tool_use_id) ||
          (result.isError !== undefined && typeof result.isError !== 'boolean')) throw new Error('validation')
    }
    for (const use of row.toolUses) {
      if (!use || typeof use.tool_use_id !== 'string' || !use.tool_use_id ||
          typeof use.tool !== 'string' || !use.input || typeof use.input !== 'object' ||
          calls.has(use.tool_use_id) || originalCalls.get(use.tool_use_id) !== use.tool) throw new Error('validation')
      calls.add(use.tool_use_id)
      pending.add(use.tool_use_id)
    }
  }
  if (pending.size) throw new Error('validation')
  return rows
}

export function compactionBridge(bridge: UsageBridge | null): UsageBridge | null {
  if (bridge === null || !/^http:\/\/(127\.0\.0\.1:\d{1,5}|localhost)\/claude-mod\/usage$/.test(bridge.url)) return null
  // The base URL has already passed usage.ts's loopback-only check. Pin the
  // path rather than allowing the request body to choose another bridge use.
  return { ...bridge, url: bridge.url.replace(/\/claude-mod\/usage$/, '/claude-mod/compact') }
}

/** Whether this spawn's bridge mounted the compaction route. DelegatedLoop
 * names the route in FLUID_CLAUDE_MOD_COMPACT_URL only when it mounts it, and
 * the name must be this bridge's own pinned route, so a stale or foreign value
 * leaves the mod on the note-only hook. A handshake, not a switch. */
export function mounted(bridge: UsageBridge | null, route: string | undefined): boolean {
  return route !== undefined && route !== '' && compactionBridge(bridge)?.url === route
}
