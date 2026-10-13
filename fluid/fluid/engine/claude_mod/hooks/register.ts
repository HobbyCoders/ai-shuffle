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
import { withDedicatedToolGuidance, withoutBashFirstSteer } from './steer.ts'
import { POST_TIMEOUT_MS, type UsageBridge, bridgeFrom, shouldPost, usageReport } from './usage.ts'

export const register: Register = (on) => {
  on('prompt.attachment', { type: 'auto_mode' }, async ($, e, next) => {
    const answer = await next(e)
    if (answer.text === null) return answer
    return { text: withoutBashFirstSteer(answer.text) }
  })

  on('tool.describe', { tool: 'Bash' }, async ($, e, next) => {
    const answer = await next(e)
    return { ...answer, description: withDedicatedToolGuidance(answer.description) }
  })

  // The main conversation's compactions, every trigger: the summarizer is
  // told what AI Shuffle puts back. A subagent's compaction, the result and a
  // skip pass through untouched; a note that cannot be built leaves the
  // compaction as the CLI would run it.
  on('session.compact', async ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
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
  let lastPostAt: number | null = null
  let postStartedAt: number | null = null

  on('session.start', async ($, e, next) => {
    const answer = await next(e)
    const url = await $.env.get('FLUID_CLAUDE_MOD_URL')
    const token = await $.env.get('FLUID_CLAUDE_MOD_TOKEN')
    const socketPath = await $.env.get('FLUID_CLAUDE_MOD_SOCKET')
    bridge = bridgeFrom(url, token, socketPath)
    try {
      if (url !== undefined) await $.env.set('FLUID_CLAUDE_MOD_URL', undefined)
      if (token !== undefined) await $.env.set('FLUID_CLAUDE_MOD_TOKEN', undefined)
      if (socketPath !== undefined) await $.env.set('FLUID_CLAUDE_MOD_SOCKET', undefined)
    } catch {
      // Left in the environment; the report still goes out.
    }
    return answer
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
