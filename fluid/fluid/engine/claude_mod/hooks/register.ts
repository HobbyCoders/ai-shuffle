// AI Shuffle's Claude Code mod, loaded with --plugin-dir by fluid's
// DelegatedLoop (fluid/fluid/engine/delegated.py) on Claude Code 2.1.287 and
// newer. It keeps the model on the dedicated file tools: the bash-first steer
// never reaches the model, and a trimmed Bash description gets its pointer to
// Read, Grep, Glob, Edit and Write back. It reads no credentials and makes no
// calls of its own; a hook that fails is skipped and the CLI's own text goes
// out, which is the behaviour without the mod.
import type { Register } from 'claude-code'
import { withDedicatedToolGuidance, withoutBashFirstSteer } from './steer.ts'

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
}
