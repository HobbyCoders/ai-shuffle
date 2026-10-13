// Offline checks for the AI Shuffle mod: `claude plugin test <this mod>`.
// The test's own hooks stand for Claude Code beneath the mod, so each case
// feeds the mod what the CLI would compute and reads what the model would get.
import { describe, expect, test } from 'claude-code/testing'
import { DEDICATED_TOOLS_LINE, withDedicatedToolGuidance, withoutBashFirstSteer } from '../hooks/steer.ts'

// Shaped like Claude Code 2.1.287's attachment: a mode header, then one
// paragraph steering file work onto Bash. The wording here is a stand-in.
const STEER =
  'While bypass permissions mode is active:\n\n' +
  'Do file work through the Bash tool: read with cat or sed -n, search with grep, and edit with ' +
  'sed or heredocs instead of the Read, Edit, or Write tools.'

const OTHER = 'Approve calls without asking; the classifier still reviews outward writes.'

const TRIMMED_BASH = 'Executes a given bash command and returns its output.\n\n- Quote paths with spaces.\n'
const FULL_BASH =
  'Executes a given bash command and returns its output.\n\n' +
  '- IMPORTANT: Avoid using this tool to run `find`, `grep`, `cat`, `head` commands. Use the dedicated tools.\n'

describe('withoutBashFirstSteer', () => {
  test('leaves nothing of an attachment that is only the steer', () => {
    expect(withoutBashFirstSteer(STEER)).toBe(null)
    expect(withoutBashFirstSteer(STEER.replace('bypass permissions mode', 'auto mode'))).toBe(null)
  })

  test('keeps the rest of an attachment that carries more than the steer', () => {
    const text = STEER + '\n\n' + OTHER
    expect(withoutBashFirstSteer(text)).toBe('While bypass permissions mode is active:\n\n' + OTHER)
  })

  test('returns other text unchanged', () => {
    expect(withoutBashFirstSteer(OTHER)).toBe(OTHER)
    expect(withoutBashFirstSteer('Use the Bash tool for git and builds.')).toBe('Use the Bash tool for git and builds.')
  })

  test('answers the same text every time', () => {
    const text = STEER + '\n\n' + OTHER
    expect(withoutBashFirstSteer(text)).toBe(withoutBashFirstSteer(text))
  })
})

describe('withDedicatedToolGuidance', () => {
  test('adds the line to a trimmed Bash description', () => {
    expect(withDedicatedToolGuidance(TRIMMED_BASH)).toBe(TRIMMED_BASH.trimEnd() + '\n' + DEDICATED_TOOLS_LINE)
  })

  test("leaves Claude Code's own guidance alone", () => {
    expect(withDedicatedToolGuidance(FULL_BASH)).toBe(FULL_BASH)
  })

  test('adds the line once', () => {
    const once = withDedicatedToolGuidance(TRIMMED_BASH)
    expect(withDedicatedToolGuidance(once)).toBe(once)
  })
})

describe('through the hooks', () => {
  test('the steer attachment never reaches the model', async ($, on) => {
    on('prompt.attachment', () => ({ text: STEER }))
    const answer = await $.prompt.attachment({ type: 'auto_mode', text: STEER, origin: { kind: 'engine' } })
    expect(answer.text).toBe(null)
  })

  test('other attachments pass through untouched', async ($, on) => {
    on('prompt.attachment', () => ({ text: 'Today is Friday.' }))
    const answer = await $.prompt.attachment({ type: 'date', text: 'Today is Friday.', origin: { kind: 'engine' } })
    expect(answer.text).toBe('Today is Friday.')
  })

  test('a trimmed Bash description gets the dedicated tools back', async ($, on) => {
    on('tool.describe', () => ({ description: TRIMMED_BASH }))
    const answer = await $.tool.describe({
      tool: 'Bash', description: TRIMMED_BASH, provider: { plugin: 'engine', tier: 'core' },
    })
    expect(answer.description).toBe(TRIMMED_BASH.trimEnd() + '\n' + DEDICATED_TOOLS_LINE)
  })

  test('other tools keep their descriptions', async ($, on) => {
    on('tool.describe', () => ({ description: 'Reads a file.' }))
    const answer = await $.tool.describe({
      tool: 'Read', description: 'Reads a file.', provider: { plugin: 'engine', tier: 'core' },
    })
    expect(answer.description).toBe('Reads a file.')
  })
})
