// Offline checks for the mod's summarizer line: `claude plugin test <this mod>`.
// The test's own `session.compact` hook stands for Claude Code's compaction
// beneath the mod: it records the instructions the summarizer would get and
// answers as the CLI would.
import type { On } from 'claude-code'
import { describe, expect, test } from 'claude-code/testing'
import { FLUID_SUMMARY_NOTE, withFluidNote } from '../hooks/compact.ts'

const TRANSCRIPT = [
  { role: 'user' as const, text: 'Fix the failing test.', toolUses: [] },
  { role: 'assistant' as const, text: 'Looking at it.', toolUses: [] },
]
const SUMMARY = [{ role: 'user' as const, text: 'Summary: the user asked to fix the failing test.', toolUses: [] }]
const KEPT = { messages: SUMMARY, tokensBefore: 150000, tokensAfter: 9000 }

/** Claude Code's compaction beneath the mod; collects what it was told. */
function engine(on: On, answer: unknown = KEPT) {
  const told: (string | undefined)[] = []
  on('session.compact', (_$, e) => {
    told.push(e.instructions)
    return answer as typeof KEPT
  })
  return told
}

describe('withFluidNote', () => {
  test('gives the line alone when the user typed nothing after /compact', () => {
    expect(withFluidNote(undefined)).toBe(FLUID_SUMMARY_NOTE)
    expect(withFluidNote('')).toBe(FLUID_SUMMARY_NOTE)
    expect(withFluidNote('  \n')).toBe(FLUID_SUMMARY_NOTE)
  })

  test("keeps the user's own text first", () => {
    expect(withFluidNote('Keep the migration plan.\n')).toBe('Keep the migration plan.\n\n' + FLUID_SUMMARY_NOTE)
  })

  test('adds the line once', () => {
    const once = withFluidNote('Keep the plan.')
    expect(withFluidNote(once)).toBe(once)
  })

  test('is the same text every time', () => {
    expect(withFluidNote(undefined)).toBe(withFluidNote(undefined))
    expect(withFluidNote('x')).toBe(withFluidNote('x'))
  })

  test('says what the owner asked for and nothing about the session', () => {
    expect(FLUID_SUMMARY_NOTE).toContain('word for word')
    expect(FLUID_SUMMARY_NOTE).toContain('Do not restate them')
    expect(FLUID_SUMMARY_NOTE.includes('\n')).toBe(false)
  })
})

describe('through the hooks', () => {
  for (const trigger of ['manual', 'auto', 'plugin', 'precompute'] as const) {
    test(`the main conversation's ${trigger} compaction tells the summarizer`, async ($, on) => {
      const told = engine(on)
      await $.session.compact({ trigger, messages: TRANSCRIPT })
      expect(told).toEqual([FLUID_SUMMARY_NOTE])
    })
  }

  test("a /compact with the user's text keeps it first", async ($, on) => {
    const told = engine(on)
    await $.session.compact({ trigger: 'manual', instructions: 'Focus on the failing test.', messages: TRANSCRIPT })
    expect(told).toEqual(['Focus on the failing test.\n\n' + FLUID_SUMMARY_NOTE])
  })

  test("a subagent's compaction is left alone", async ($, on) => {
    const told = engine(on)
    await $.session.compact({ trigger: 'auto', agentId: 'agent-1', messages: TRANSCRIPT })
    await $.session.compact({ trigger: 'auto', agentId: 'agent-1', instructions: 'Theirs.', messages: TRANSCRIPT })
    expect(told).toEqual([undefined, 'Theirs.'])
  })

  test('the compaction comes back as the CLI made it', async ($, on) => {
    engine(on)
    const answer = await $.session.compact({ trigger: 'auto', messages: TRANSCRIPT })
    expect(answer).toEqual(KEPT)
  })

  test('a skip passes through', async ($, on) => {
    engine(on, { skip: 'blocked by a PreCompact hook' })
    const answer = await $.session.compact({ trigger: 'manual', messages: TRANSCRIPT })
    expect(answer).toEqual({ skip: 'blocked by a PreCompact hook' })
  })

  test('every compaction gets the same bytes', async ($, on) => {
    const told = engine(on)
    await $.session.compact({ trigger: 'auto', messages: TRANSCRIPT })
    await $.session.compact({ trigger: 'auto', messages: TRANSCRIPT })
    expect(told[0]).toBe(told[1])
  })
})
