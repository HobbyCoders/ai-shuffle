// What the mod tells Claude Code's summarizer when the conversation compacts.
// A pure text function, so the hooks module stays a thin binding and the tests
// run it directly.
//
// After a compaction AI Shuffle puts its own context back itself (the session
// ledger through the SessionStart hook, its turn note with the next message),
// so the summary need not restate it. What only the summary can keep is the
// user's own words: mid-turn, the request the model is working on survives
// only as the summary's paraphrase. The CLI sends the instructions in the
// summarizer request's last user message, after the conversation it reuses as
// its cached prefix, so the fixed line below never changes a cached prefix
// (docs/reviews/claude-mods-compaction-2026-10-04.md, sections 5.3 and 8).

/** The line every compaction of the main conversation gets, always the same bytes. */
export const FLUID_SUMMARY_NOTE =
  'AI Shuffle puts its own reminders back after this summary: saved memory, the environment note, ' +
  'Away mode and the session ledger. Do not restate them. Quote the user\'s most recent request word ' +
  'for word, and keep open questions, decisions and promised follow-ups.'

/**
 * The summarizer's instructions with AI Shuffle's line added: the user's own
 * `/compact` text first (less trailing blanks), then the line; the line alone when
 * there is no text; unchanged when the line is already there or the input is
 * not text.
 */
export function withFluidNote(instructions: string | undefined): string | undefined {
  if (instructions === undefined || instructions === null) return FLUID_SUMMARY_NOTE
  if (typeof instructions !== 'string') return instructions
  if (instructions.includes(FLUID_SUMMARY_NOTE)) return instructions
  if (instructions.trim() === '') return FLUID_SUMMARY_NOTE
  return instructions.replace(/\s+$/, '') + '\n\n' + FLUID_SUMMARY_NOTE
}
