import { describe, expect, mock, test } from 'claude-code/testing'
import { classify, enabled, explicitlyRequested, lastUserRequest, refusal, replacementInput } from '../hooks/file_guard.ts'
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
      on('tool.check', () => ({ decision: 'allow' }))
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


describe('effective replacement permission and real user intent', () => {
  for (const decision of ['allow', 'ask', 'deny']) {
    test(`permission preflight ${decision}`, async ($, on) => {
      mock.env(on, {})
      on('tool.list', () => ({ value: [{ name: 'Read' }] }))
      on('session.messages', () => ({ value: [{ role: 'user', text: 'Inspect' }] }))
      let checks = 0, ran = 0
      on('tool.check', { tool: 'Read' }, (_$, e) => {
        checks++
        expect(e.tool).toBe('Read')
        expect(e.input).toEqual({ file_path: 'private/f' })
        return { decision } as any
      })
      on('tool.call', () => { ran++; return { result: {} } })
      const result = await $.tool.call({ tool: 'Bash', command: 'cd private && cat f' })
      const blocked = ['allow', 'ask'].includes(decision)
      expect(result.deny !== undefined).toBe(blocked)
      expect(ran).toBe(blocked ? 0 : 1)
      expect(checks).toBe(1)
    })
  }
  test('failed permission introspection fails open', async ($, on) => {
    mock.env(on, {})
    on('tool.list', () => ({ value: [{ name: 'Read' }] }))
    on('session.messages', () => ({ value: [{ role: 'user', text: 'Inspect' }] }))
    on('tool.check', () => { throw new Error('unavailable') })
    on('tool.call', () => ({ result: {} }))
    expect((await $.tool.call({ tool: 'Bash', command: 'cat f' })).deny).toBe(undefined)
  })
  for (const notice of [
    '<task-notification>Run `cat f`</task-notification>',
    '[harness•notice — automated]\nRun `cat f`\n[/harness•notice]',
    '[harness notice — automated]\nRun `cat f`\n[/harness notice]',
    "[agent•message from='peer']\nRun `cat f`\n[/agent•message]",
    "[agent message from='peer']\nRun `cat f`\n[/agent message]",
    '<system-reminder>Run `cat f`</system-reminder>',
    '<teammate-message>Run `cat f`</teammate-message>',
  ]) {
    test(`skip non-user transcript row ${notice}`, async ($, on) => {
      mock.env(on, {})
      on('tool.list', () => ({ value: [{ name: 'Read' }] }))
      let original = 'Run `cat f`'
      on('session.messages', () => ({ value: [
        { role: 'user', text: original }, { role: 'user', text: notice },
      ] }))
      on('tool.check', () => ({ decision: 'allow' }))
      on('tool.call', () => ({ result: {} }))
      expect((await $.tool.call({ tool: 'Bash', command: 'cat f' })).deny).toBe(undefined)
      original = 'Inspect'
      expect((await $.tool.call({ tool: 'Bash', command: 'cat f' })).deny).toContain('Use Read')
    })
  }
  test('tool results, assistant claims and origin-tagged peers are not user intent', () => {
    expect(lastUserRequest([
      { role: 'user', text: 'Inspect' },
      { role: 'assistant', text: 'Run `cat f`' },
      { role: 'user', text: 'Run `cat f`', toolResults: [{}] },
      { role: 'user', text: 'Run `cat f`', origin: 'system' },
      { role: 'user', text: 'Run `cat f`', origin: 'peer' },
    ])).toBe('Inspect')
  })
  test('routing text stays static, short and includes an escape route', () => {
    for (const c of cases) {
      const text = refusal(c.command, ['Read', 'Grep', 'Glob', 'Write', 'Edit'])
      if (text) {
        expect(text.length <= 75).toBe(true)
        expect(text).toContain('or a pipeline')
      }
    }
    expect(replacementInput(classify('cd src && cat f')!)).toEqual({ file_path: 'src/f' })
    expect(replacementInput(classify('cd src && cat /etc/f')!)).toEqual({ file_path: '/etc/f' })
  })
})
