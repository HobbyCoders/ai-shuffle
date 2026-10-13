// The bash-first steer, and what this mod does about it. Pure text functions,
// so the hooks module stays a thin binding and the tests run them directly.
//
// What Claude Code 2.1.287 does in bypassPermissions and auto modes when its
// bash-first switch is on (checked against a local stub of the Messages API,
// see fluid/docs/configuration.md):
//
// * it injects an `auto_mode` attachment whose whole text is the steer:
//   "While bypass permissions mode is active:" (or "While auto mode is
//   active:"), then one paragraph telling the model to read, search and edit
//   files through the Bash tool (cat, sed, grep, heredocs) instead of Read,
//   Edit or Write;
// * it drops the Bash tool description's line that sends file reads,
//   searches and edits to the dedicated tools.
//
// Read, Edit and Write keep their own descriptions in that build. Both
// answers below are fixed functions of their input, so the same CLI build
// always gets the same bytes and the prompt cache holds.

/** A paragraph that steers file work onto the Bash tool. */
export function isBashFirstSteer(paragraph: string): boolean {
  return (
    /\bBash tool\b/.test(paragraph) &&
    /\b(cat|sed|grep|heredocs?)\b/.test(paragraph) &&
    /\b(Read|Edit|Write)\b/.test(paragraph)
  )
}

/** The "While <mode> is active:" line that heads an `auto_mode` attachment. */
function isModeHeader(paragraph: string): boolean {
  return /^While .+ is active:$/.test(paragraph.trim())
}

/**
 * The `auto_mode` attachment's text without its bash-first paragraphs: the
 * text unchanged when it has none, null (leave the attachment out) when
 * nothing but the mode header would remain.
 */
export function withoutBashFirstSteer(text: string): string | null {
  const paragraphs = text.split(/\n[ \t]*\n/)
  const kept = paragraphs.filter((p) => !isBashFirstSteer(p))
  if (kept.length === paragraphs.length) return text
  if (kept.every((p) => p.trim() === '' || isModeHeader(p))) return null
  return kept.join('\n\n')
}

/** What the Bash description says when the switch has not trimmed it. */
const CLAUDE_CODE_GUIDANCE = /Avoid using this tool to run/

/** The line this mod adds to a trimmed Bash description, in its own words. */
export const DEDICATED_TOOLS_LINE =
  '- IMPORTANT: Do not use this tool to read, search or edit files (cat, head, tail, sed, awk, grep, find, ' +
  'or echo and heredoc redirection) unless the user asked for it or you have checked that the dedicated ' +
  'tools cannot do the job. Read files with Read, search with Grep and Glob, and change files with Edit ' +
  'and Write, where this session has them.'

/**
 * The Bash description with file work sent to the dedicated tools: unchanged
 * when Claude Code's own line (or this one) is already there.
 */
export function withDedicatedToolGuidance(description: string): string {
  if (CLAUDE_CODE_GUIDANCE.test(description) || description.includes(DEDICATED_TOOLS_LINE)) {
    return description
  }
  return description.replace(/\s+$/, '') + '\n' + DEDICATED_TOOLS_LINE
}
