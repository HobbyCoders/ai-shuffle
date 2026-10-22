// The compaction bridge response is untrusted until its handles, roles and tool
// pairs validate. A rejected response never changes the event passed to next.
import type { UsageBridge } from './usage.ts'

type Row = {
  role: 'user' | 'assistant'; text: string; handle?: string
  toolUses: { tool_use_id: string; tool: string; input: Record<string, unknown> }[]
  toolResults?: { tool_use_id: string; text: string; isError?: boolean }[]
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
    if (!row || (row.role !== 'user' && row.role !== 'assistant') || row.role === previous ||
        typeof row.text !== 'string' || !Array.isArray(row.toolUses) ||
        (row.toolResults !== undefined && !Array.isArray(row.toolResults))) throw new Error('validation')
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
    if (pending.size) throw new Error('validation')
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
