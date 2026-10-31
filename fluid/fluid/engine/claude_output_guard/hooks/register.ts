// Separate from the optional guidance/compaction mod. Classic PostToolUse
// rewrites successes only; session.append covers errors too, before Claude Code
// adds host hook context, steering, and its own reminders to the model input.
import type { Register } from 'claude-code'
import { defangToolMessage } from './tool_output.ts'

export const register: Register = (on) => {
  on('session.append', async ($, e, next) => next({ ...e, message: defangToolMessage(e.message) }))

  // A listed plugin is not proof its function hooks loaded. The host accepts
  // only this process's nonce at its own temporary receipt path, not text from
  // a tool, transcript, or model. File I/O also works with nonessential network
  // traffic disabled. Remove these values before any tool process can inherit.
  on('session.start', async ($, e, next) => {
    const receipt = await $.env.get('FLUID_TOOL_OUTPUT_GUARD_RECEIPT')
    const nonce = await $.env.get('FLUID_TOOL_OUTPUT_GUARD_NONCE')
    await $.env.set('FLUID_TOOL_OUTPUT_GUARD_RECEIPT', undefined)
    await $.env.set('FLUID_TOOL_OUTPUT_GUARD_NONCE', undefined)
    if (receipt && nonce) await $.fs.write(receipt, nonce)
    return next(e)
  })
}
