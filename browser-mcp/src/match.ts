/**
 * Server-side pattern matching for browser_console (required `pattern`) and
 * browser_network (`url_pattern`). A pattern is treated as a regular expression
 * when it compiles, otherwise as a plain substring. Most literal substrings are
 * also valid regexes, so this transparently supports both; only genuinely
 * malformed regex syntax (e.g. an unclosed "[") falls back to substring.
 *
 * ReDoS note: patterns come from tool callers and run server-side on the event
 * loop with no native regex timeout, so a catastrophically-backtracking pattern
 * could stall the loop (and the session mutex queue). JS offers no per-regex
 * timeout; our mitigation is a length cap — an over-length pattern is NOT
 * compiled to a RegExp at all and is matched as a plain substring instead
 * (still useful, and immune to backtracking). Matching is otherwise bounded to
 * ≤500 buffered entries, so the residual risk is a single over-long input
 * string, which the length cap on the pattern also bounds.
 */

/** Patterns longer than this are treated as literal substrings, never regex. */
export const PATTERN_MAX_LEN = 200;

export function compileMatcher(pattern: string): (value: string) => boolean {
  if (pattern.length <= PATTERN_MAX_LEN) {
    try {
      const re = new RegExp(pattern);
      return (value: string) => re.test(value);
    } catch {
      /* not a valid regex — fall through to substring matching */
    }
  }
  return (value: string) => value.includes(pattern);
}
