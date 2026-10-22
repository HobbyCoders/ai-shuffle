// AI Shuffle's Claude Code mod, loaded with --plugin-dir by fluid's
// DelegatedLoop (fluid/fluid/engine/delegated.py) on Claude Code 2.1.287 and
// newer. It keeps the model on the dedicated file tools: the bash-first steer
// never reaches the model, and a trimmed Bash description gets its pointer to
// Read, Grep, Glob, Edit and Write back. It also reports the plan usage the
// CLI measures to AI Shuffle over the session's loopback bridge (usage.ts),
// and tells the summarizer what AI Shuffle puts back after a compaction
// (compact.ts). It reads no credentials and never calls the API; a hook that
// fails is skipped and the CLI's own behaviour goes on, as without the mod.
import type { Register } from 'claude-code'
import { withFluidNote } from './compact.ts'
import { classify, enabled, lastUserRequest, refusal, replacementInput } from './file_guard.ts'
import { compactedRows, compactionBridge } from './mechanical.ts'
import { withDedicatedToolGuidance, withoutBashFirstSteer } from './steer.ts'
import { POST_TIMEOUT_MS, type UsageBridge, bridgeFrom, shouldPost, usageReport } from './usage.ts'

export const register: Register = (on) => {
  // tool.call wraps execution, not permission prompting: bypassPermissions
  // still comes through here. Unknown syntax or unavailable introspection
  // passes unchanged. This is routing guidance, not a security boundary.
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    // tool.list/check queries are main-loop scoped. Never apply that catalog
    // or permission verdict to an agent whose effective surface may differ.
    if (e.agentId) return next(e)
    let guidance: string | null = null
    try {
      if (!enabled(await $.env.get('FLUID_BASH_FILE_GUARD'))) return next(e)
      const found = classify(e.command)
      if (!found) return next(e)
      const catalog = await $.tool.list()
      const messages = await $.session.messages()
      if (!Array.isArray(catalog) || !Array.isArray(messages)) return next(e)
      guidance = refusal(e.command, catalog.map(t => t.name), lastUserRequest(messages))
      if (!guidance) return next(e)
      // list() alone is NOT evidence of permission. This documented dry query
      // evaluates the current mode/rules without execution, dialogs or hooks
      // such as PreToolUse. A missing/denied/unknown verdict fails open.
      const verdict = await $.tool.check({ tool: found.tool, input: replacementInput(found) })
      if (!['allow', 'ask'].includes(verdict?.decision)) return next(e)
    } catch {
      return next(e)
    }
    return { deny: guidance! }
  })

  on('prompt.attachment', { type: 'auto_mode' }, async ($, e, next) => {
    const answer = await next(e)
    if (answer.text === null) return answer
    return { text: withoutBashFirstSteer(answer.text) }
  })

  on('tool.describe', { tool: 'Bash' }, async ($, e, next) => {
    const answer = await next(e)
    return { ...answer, description: withDedicatedToolGuidance(answer.description) }
  })

  // Off: the original note-only hook, unchanged. On: the main conversation
  // compacts through Fluid, or falls back with the original event. Precompute
  // is skipped so it cannot spend a summarizer request ahead of the threshold.
  // Subagents always pass through untouched.
  on('session.compact', async ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
    // A sufficiently large --resume may compact before session.start itself.
    // Capture lazily in that case; an off switch still takes the old branch.
    if (!bridgeInitialized) {
      try {
        const flag = await $.env.get('FLUID_CLAUDE_MOD_COMPACTION')
        if (['1', 'true', 'yes', 'on'].includes((flag ?? '').trim().toLowerCase())) {
          mechanical = true
          bridge = bridgeFrom(await $.env.get('FLUID_CLAUDE_MOD_URL'),
            await $.env.get('FLUID_CLAUDE_MOD_TOKEN'), await $.env.get('FLUID_CLAUDE_MOD_SOCKET'))
          bridgeInitialized = true
          await $.env.set('FLUID_CLAUDE_MOD_URL', undefined)
          await $.env.set('FLUID_CLAUDE_MOD_TOKEN', undefined)
          await $.env.set('FLUID_CLAUDE_MOD_SOCKET', undefined)
          await $.env.set('FLUID_CLAUDE_MOD_COMPACTION', undefined)
        }
      } catch {
        try { await $.ui.log('Fluid compaction fallback: bridge initialization error', { to: 'debug' }) } catch { /* Best effort. */ }
        return next(e)
      }
    }
    if (mechanical) {
      if (e.trigger === 'precompute') return { skip: 'Fluid compacts mechanically at the threshold' }
      let reason = 'bridge unavailable'
      try {
        const target = compactionBridge(bridge)
        if (target === null) throw new Error('bridge')
        reason = 'bridge transport error'
        const response = await Promise.race([
          $.http.fetch(target.url, {
            method: 'POST',
            headers: { 'content-type': 'application/json', authorization: `Bearer ${target.token}` },
            body: JSON.stringify({ v: 1, messages: e.messages }),
            ...(target.socketPath ? { socketPath: target.socketPath } : {}),
          }),
          $.clock.sleep(POST_TIMEOUT_MS).then(() => { reason = 'bridge timeout'; throw new Error('timeout') }),
        ])
        if (!response.ok || response.status !== 200) {
          reason = `bridge HTTP ${response.status}`
          throw new Error('status')
        }
        reason = 'invalid bridge response'
        return { messages: compactedRows(JSON.parse(response.text), e.messages) }
      } catch {
        try { await $.ui.log(`Fluid compaction fallback: ${reason}`, { to: 'debug' }) } catch { /* Best effort. */ }
        return next(e)
      }
    }
    let instructions = e.instructions
    try {
      instructions = withFluidNote(e.instructions)
    } catch {
      // Unchanged.
    }
    return next(instructions === e.instructions ? e : { ...e, instructions })
  })

  // Usage reporting. The bridge is read once, then taken out of the
  // environment so the commands the model runs do not inherit its token.
  let bridge: UsageBridge | null = null
  let mechanical = false
  let bridgeInitialized = false
  let lastPostAt: number | null = null
  let postStartedAt: number | null = null

  on('session.start', async ($, e, next) => {
    if (bridgeInitialized) return next(e)
    // Resuming a large transcript can compact inside next(e), before init is
    // printed. Capture the bridge first, not after that compaction has run.
    const url = await $.env.get('FLUID_CLAUDE_MOD_URL')
    const token = await $.env.get('FLUID_CLAUDE_MOD_TOKEN')
    const socketPath = await $.env.get('FLUID_CLAUDE_MOD_SOCKET')
    bridge = bridgeFrom(url, token, socketPath)
    const compact = await $.env.get('FLUID_CLAUDE_MOD_COMPACTION')
    mechanical = ['1', 'true', 'yes', 'on'].includes((compact ?? '').trim().toLowerCase())
    bridgeInitialized = true
    try {
      if (url !== undefined) await $.env.set('FLUID_CLAUDE_MOD_URL', undefined)
      if (token !== undefined) await $.env.set('FLUID_CLAUDE_MOD_TOKEN', undefined)
      if (socketPath !== undefined) await $.env.set('FLUID_CLAUDE_MOD_SOCKET', undefined)
      if (compact !== undefined) await $.env.set('FLUID_CLAUDE_MOD_COMPACTION', undefined)
    } catch {
      // Left in the environment; the report still goes out.
    }
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const answer = await next(e)
    const target = bridge
    if (target === null || e.rateLimits.length === 0) return answer
    const now = await $.clock.now()
    if (postStartedAt !== null && now - postStartedAt < POST_TIMEOUT_MS) return answer
    if (!shouldPost(e.changed, now, lastPostAt)) return answer
    const body = usageReport(e.rateLimits, now)
    if (body === null) return answer
    lastPostAt = now
    postStartedAt = now
    // Not awaited: the turn never waits on AI Shuffle, and a failed post is
    // dropped (the next turn's measurement posts again).
    $.http
      .fetch(target.url, {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${target.token}` },
        body,
        ...(target.socketPath ? { socketPath: target.socketPath } : {}),
      })
      .catch(() => undefined)
      .finally(() => {
        postStartedAt = null
      })
    return answer
  })
}
