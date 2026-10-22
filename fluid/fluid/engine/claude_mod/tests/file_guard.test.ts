import { describe, expect, mock, test } from 'claude-code/testing'
import { classify, enabled, explicitlyRequested, refusal } from '../hooks/file_guard.ts'
import cases from './file_guard_cases.ts'

describe('shared Python/TS classifier corpus', () => {
  for (const c of cases) {
    test(c.command, () => {
      expect(classify(c.command)?.tool ?? null).toBe(c.tool)
      expect(refusal(c.command, [])).toBe(null)
      if (c.tool) expect(refusal(c.command, [c.tool])).toContain('Use ' + c.tool)
    })
  }
  test('exact user literals are conservatively exempt', () => {
    for (const text of ['cat f', 'Run `cat f`', 'Please execute exactly: `cat f`.', 'Run cat f', 'Run:\n```bash\ncat f\n```']) {
      expect(explicitlyRequested('cat f', text)).toBe(true)
      expect(refusal('cat f', ['Read'], text)).toBe(null)
    }
    expect(explicitlyRequested('cat f', 'For example: `cat f`')).toBe(true)
  })
  test('kill switch defaults on', () => {
    expect(enabled(undefined)).toBe(true)
    for (const value of ['', '0', 'false', 'off', 'no']) expect(enabled(value)).toBe(false)
  })
})

describe('tool.call pre-execution gate (no permission prompt needed)', () => {
  for (const scenario of [
    { tools: ['Bash', 'Read'], command: 'cat f', user: 'Inspect the file', blocked: true },
    { tools: ['Bash'], command: 'cat f', user: 'Inspect the file', blocked: false },
    { tools: ['Bash', 'Read'], command: 'cat f', user: 'Run `cat f`', blocked: false },
    { tools: ['Bash', 'Read'], command: 'git status', user: 'Inspect the file', blocked: false },
    { tools: ['Bash', 'Read'], command: 'cat f | python', user: 'Inspect the file', blocked: false },
    { tools: ['Bash', 'Read'], command: 'cat f', user: 'Inspect the file', blocked: false, off: true },
  ]) {
    test(JSON.stringify(scenario), async ($, on) => {
      mock.env(on, scenario.off ? { FLUID_BASH_FILE_GUARD: 'false' } : {})
      on('tool.list', () => ({ value: scenario.tools.map(name => ({ name })) }))
      on('session.messages', () => ({ value: [{ role: 'user', text: scenario.user, toolUses: [] }] }))
      let ran = 0
      on('tool.call', () => { ran++; return { result: {} } })
      const result = await $.tool.call({ tool: 'Bash', command: scenario.command })
      expect(result.deny !== undefined).toBe(scenario.blocked)
      expect(ran).toBe(scenario.blocked ? 0 : 1)
      if (scenario.blocked) expect(result.deny).toContain('Use Read')
    })
  }
  test('agent introspection failure allows rather than overrules the user', async ($, on) => {
    mock.env(on, {})
    on('tool.list', () => ({ value: [{ name: 'Read' }] }))
    on('session.messages', () => ({ value: { deny: 'unreadable agent' } }))
    on('tool.call', () => ({ result: {} }))
    const result = await $.tool.call({ tool: 'Bash', command: 'cat f', agentId: 'a1' })
    expect(result.deny).toBe(undefined)
  })
})
