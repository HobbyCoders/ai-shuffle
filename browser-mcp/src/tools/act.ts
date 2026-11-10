/**
 * browser_act: single action tool. `kind` selects the gesture; the target is
 * exactly one of `ref` (preferred) or `coordinate` [x,y] in the latest
 * screenshot's pixel space (optionally bound to it by `screenshot_id`). Result:
 *   { ok, url_changed, url?, dom_mutated: none|minor|major, snapshot_stale, events? }
 * `events` lists tabs, downloads and dialogs the page produced during the
 * action. A fresh interactive snapshot is attached only when the DOM changed
 * majorly or a navigation occurred (otherwise existing refs remain valid).
 * Failures carry `outcome`: not_executed (safe to fix and retry) or unknown
 * (input was sent; observe before anything else, never auto-retried).
 */

import type { CDPSession, Locator, Page } from "playwright-core";
import { StaleRefError, raceWithTimeout, type Session } from "../session.js";
import { jsonResult, errorResult, errorCode, text, type McpToolResult } from "./result.js";
import { CAPTCHA_AGENT_NOTE } from "../captcha.js";
import { ACT_KINDS } from "../schemas.js";
import { endRequestWatch } from "../dommutation.js";
import { runScreenshot } from "./screenshot.js";

type Kind = (typeof ACT_KINDS)[number];

/**
 * Actionability-wait budget for ref-targeted actions. Short on purpose: a ref
 * whose element detached (a minor mutation, no navigation) resolves to a live
 * locator that would otherwise block for Playwright's 30s default before
 * failing. We fail fast and remap the timeout to a clean stale_ref instead.
 */
const REF_ACTION_TIMEOUT_MS = 2000;

/**
 * Per-character allowance for `type` on a ref. Playwright's pressSequentially
 * timeout covers the keystrokes as well as the actionability wait, at roughly
 * 1ms per character on an idle host; a flat 2s cut a 3000-character value off
 * part-typed and reported it as stale_ref. Generous so a loaded host still
 * finishes; the actionability part of the budget stays REF_ACTION_TIMEOUT_MS.
 */
export const TYPE_MS_PER_CHAR = 25;

/** Timeout for typing `value` into a ref: the actionability wait plus the keystrokes. */
export function typeTimeoutMs(value: string): number {
  return REF_ACTION_TIMEOUT_MS + value.length * TYPE_MS_PER_CHAR;
}

/** Budget for asking whether a ref still resolves after its action failed. */
const REF_PROBE_TIMEOUT_MS = 1000;
const EVIDENCE_TIMEOUT_MS = 500;
const MAX_FILL_VALUE_ECHO = 500;

/**
 * Could this Playwright error mean the ref no longer resolves (element detached
 * after the ref was handed out)? Such failures surface as an actionability
 * timeout naming the `aria-ref=` selector. So do failures on a ref that DOES
 * still resolve (covered, animating, not editable…), which is why a match is
 * only a reason to probe the ref, not a verdict. Coordinate (page.mouse)
 * gestures do no actionability wait, so they never hang.
 */
function isRefResolutionFailure(message: string): boolean {
  return /Timeout \d+ms exceeded/i.test(message) || message.includes("aria-ref=");
}

/**
 * Does the ref's locator still match a connected element? Playwright's
 * aria-ref engine answers from the latest snapshot and requires the element to
 * be attached, and `count()` does not wait. Any failure (the document went away
 * mid-probe, a closed page) counts as "no".
 */
async function refStillResolves(locator: Locator): Promise<boolean> {
  try {
    return (await raceWithTimeout(locator.count(), REF_PROBE_TIMEOUT_MS, "ref probe timed out")) > 0;
  } catch {
    return false;
  }
}

// Playwright colours its call log with ANSI escapes.
const ANSI_RE = /\x1b\[[0-9;]*m/g;
/**
 * A line of Playwright's that names why an action could not proceed. The reason
 * ends the line: a line that only quotes the page ends in a tag, so an element
 * whose label reads like a reason does not count.
 */
const BLOCKER_RE = /(?:intercepts pointer events|element is not \w+|element is outside of the viewport|not attached to the DOM)$/i;
const MAX_REASON_CHARS = 240;
/** Call-log line Playwright writes just before it sends a pointer gesture's events. */
const PERFORMING_RE = /^\s*-\s*performing \S+(?: and \S+)? action$/;
/**
 * Call-log lines for the actions that have no "about to send" line: fill and
 * select, which check the element and set its value in one page call. Each
 * marks the call as started, so it says the input went out only when the call
 * then got no answer. Key presses track what they sent themselves.
 */
const STARTED_RE = /^\s*-\s*waiting for element to be visible(?:, enabled and editable| and enabled)$/;
/** Call-log line of the empty type that focuses a ref before its keys (focusForKeys). */
const FOCUS_STARTED_RE = /^\s*-\s*elementHandle\.type\(""\)$/;
/** Playwright's own budget ran out: the call got no answer. Only its headline says so. */
const TIMEOUT_RE = /^\S+: Timeout \d+ms exceeded\./;
/** Call-log line Playwright writes once the page took a pointer gesture on its target. */
const GESTURE_DONE_RE = /^\s*-\s*\S+(?: and \S+)? action done$/;
/** Call-log line that opens another attempt. A repeated one carries a count in place of the dash. */
const RETRYING_RE = /^\s*(?:-|\d+ ×)\s*retrying /;

/** Text of the caller's that Playwright quotes back: what it writes before the text, the text, and after it. */
type Echo = readonly [before: string, text: string, after: string];

/** The chord a key action presses, as Playwright receives it. */
function keyChord(input: ActInput): string {
  return [...(input.modifiers ?? []), input.key ?? ""].join("+");
}

/** The keys of a chord, split as Playwright splits it: a "+" that begins a key is part of that key. */
function chordKeys(chord: string): string[] {
  const keys: string[] = [];
  let key = "";
  for (const char of chord) {
    if (char === "+" && key) { keys.push(key); key = ""; }
    else key += char;
  }
  return [...keys, key];
}

/** Does this failure open with Playwright refusing `key`? */
function refusesKey(message: string, key: string): boolean {
  const at = message.indexOf(": ");
  return at >= 0 && message.startsWith(`Unknown key: "${key}"`, at + 2);
}

/**
 * Did a chord Playwright refused already press some of its keys? Playwright
 * presses a chord key by key and stops at the first key it does not know, with
 * the keys before that one down. Nothing went out only when the failure names
 * the chord's first key and can be naming no other.
 */
function chordPartlyPressed(message: string, chord: string): boolean {
  const [first, ...rest] = chordKeys(chord);
  return !refusesKey(message, first) || rest.some(key => key !== first && refusesKey(message, key));
}

/**
 * The keys of a chord `page.keyboard.press` left down when it failed. It
 * presses them in order and stops at the first key it does not know, with the
 * keys before it down; the last key the failure could be naming is used, so an
 * ambiguous name releases more rather than less. It has no timeout, so any
 * other failure (the page went away) may have stopped at any key.
 */
function keysLeftDown(message: string, chord: string): string[] {
  const keys = chordKeys(chord);
  for (let at = keys.length - 1; at >= 0; at--) if (refusesKey(message, keys[at])) return keys.slice(0, at);
  return keys;
}

/**
 * How long a call waits for its keys to be released. A page that is still
 * blocked gets the keyups once it is free; the call does not wait for that.
 */
const KEY_RELEASE_WAIT_MS = 250;

/**
 * Release keys, last pressed first. Playwright counts a key as up as soon as it
 * receives the release, before the page answers, so input sent after this
 * carries none of them even while their keyups wait on a blocked page. It never
 * throws, so a failed release cannot replace the action's own error.
 */
async function releaseKeys(page: Page, keys: string[]): Promise<void> {
  const ups = [...keys].reverse().map(key => {
    try { return page.keyboard.up(key).catch(() => undefined); } catch { return undefined; }
  });
  await raceWithTimeout(Promise.all(ups), KEY_RELEASE_WAIT_MS, "key release timed out").catch(() => undefined);
}

const errorText = (err: unknown): string => err instanceof Error ? err.message : String(err);

/** Time left before `deadline`, as a Playwright timeout (0 would mean none). */
const timeLeft = (deadline: number): number => Math.max(1, Math.ceil(deadline - performance.now()));

/**
 * Focus a ref the way Playwright's press and type do (an input that was not
 * focused gets its caret reset), before any key goes out. A press or type that
 * then times out on a blocked page was blocked by a key, not by the page's
 * focus handler, so the keys it may have left down are known (#1423). A focus
 * Playwright began and never heard back from may have run the page's focus
 * handler, so it counts as sent, as it did when the press focused.
 */
async function focusForKeys(locator: Locator, timeout: number, markSent: () => void): Promise<void> {
  try {
    await locator.pressSequentially("", { timeout });
  } catch (err) {
    const lines = failureLines(errorText(err));
    if (TIMEOUT_RE.test(lines[0] ?? "") && lines.some(line => FOCUS_STARTED_RE.test(line))) markSent();
    throw err;
  }
}

/** The main frame's id for each CDP session whose Page events are on. */
const pageEventsOn = new WeakMap<CDPSession, Promise<string>>();

/**
 * Wait for a navigation a key press started, the way Playwright's own press
 * does in Chromium: the renderer reports a navigation it was asked for
 * (Page.frameRequestedNavigation) while it handles the key, so once the keys
 * are sent, one more Page call returns after any such report. A main-frame
 * navigation is then waited for until it commits, stays in the document or
 * stops loading. Without a CDP session the press does not wait, as a press
 * without a ref never does.
 */
async function watchNavigation(session: Session, deadline: number): Promise<{ settle(): Promise<void>; stop(): void } | undefined> {
  let cdp: CDPSession;
  try { cdp = await session.currentCDP(); } catch { return undefined; }
  let main = pageEventsOn.get(cdp);
  if (!main) {
    main = Promise.all([cdp.send("Page.enable"), cdp.send("Target.getTargetInfo")]).then(([, info]) => info.targetInfo.targetId);
    pageEventsOn.set(cdp, main);
    main.catch(() => pageEventsOn.delete(cdp));
  }
  // A page that does not answer here has had no key yet: the press times out
  // with nothing sent.
  const frame = await raceWithTimeout(main, timeLeft(deadline), `keyboard.press: Timeout ${REF_ACTION_TIMEOUT_MS}ms exceeded.`);
  let requested = false;
  let done = false;
  let wake = () => {};
  const onRequest = (event: { frameId: string }) => { if (event.frameId === frame) { requested = true; done = false; } };
  const onEnd = (frameId: string) => { if (requested && frameId === frame) { done = true; wake(); } };
  const onCommit = (event: { frame: { id: string } }) => onEnd(event.frame.id);
  const onFrameEvent = (event: { frameId: string }) => onEnd(event.frameId);
  cdp.on("Page.frameRequestedNavigation", onRequest);
  cdp.on("Page.frameNavigated", onCommit);
  cdp.on("Page.navigatedWithinDocument", onFrameEvent);
  cdp.on("Page.frameStoppedLoading", onFrameEvent);
  const timeout = `keyboard.press: Timeout ${REF_ACTION_TIMEOUT_MS}ms exceeded.`;
  return {
    async settle() {
      await raceWithTimeout(cdp.send("Page.enable"), timeLeft(deadline), timeout);
      if (!requested || done) return;
      await raceWithTimeout(new Promise<void>(resolve => { wake = resolve; }), timeLeft(deadline), `${timeout} The navigation the key started had not committed.`);
    },
    stop() {
      cdp.off("Page.frameRequestedNavigation", onRequest);
      cdp.off("Page.frameNavigated", onCommit);
      cdp.off("Page.navigatedWithinDocument", onFrameEvent);
      cdp.off("Page.frameStoppedLoading", onFrameEvent);
    },
  };
}

/**
 * Press a chord on a ref, leaving none of its keys down when it fails
 * (#1423). Playwright's own press gives up on a page that stops answering with
 * the keys it had pressed still down, says nothing of how far it got, and
 * focuses the element again first, which a page can block on before any key
 * goes out. So the ref is focused once, then every key goes down and up at page
 * level in Playwright's order, each recorded before it is sent: keys follow the
 * page's own focus changes between them, as they do in Playwright's press, and
 * a failure releases exactly the keys whose keydown went out, without waiting
 * on a page that is still blocked. A navigation the keys started is waited for
 * as Playwright's press waits for it.
 */
async function pressOnRef(session: Session, page: Page, locator: Locator, chord: string, markSent: () => void): Promise<void> {
  const deadline = performance.now() + REF_ACTION_TIMEOUT_MS;
  await focusForKeys(locator, REF_ACTION_TIMEOUT_MS, markSent);
  const navigation = await watchNavigation(session, deadline);
  const send = (verb: "down" | "up", key: string) => raceWithTimeout(page.keyboard[verb](key), timeLeft(deadline), `keyboard.${verb}: Timeout ${REF_ACTION_TIMEOUT_MS}ms exceeded.`);
  const down: string[] = [];
  try {
    for (const key of chordKeys(chord)) {
      markSent();
      down.push(key);
      try {
        await send("down", key);
      } catch (err) {
        if (refusesKey(errorText(err), key)) down.pop();
        throw err;
      }
    }
    // Playwright counts a key as up once it sends the keyup, so it leaves the
    // list first and a keyup that then times out is never sent again.
    while (down.length) await send("up", down.pop()!);
    await navigation?.settle();
  } finally {
    navigation?.stop();
    await releaseKeys(page, down);
  }
}

/**
 * Type into a ref one character per call. Playwright's pressSequentially gives
 * up on a page that stops answering with the key it was pressing still down
 * (#1423). A call of its own for each character has no timeout of
 * Playwright's, so Playwright finishes the key in flight (its keyup goes out
 * once the page answers) while this stops the text there.
 */
async function typeOnRef(page: Page, locator: Locator, value: string, markSent: () => void): Promise<void> {
  const budget = typeTimeoutMs(value);
  const deadline = performance.now() + budget;
  await focusForKeys(locator, budget, markSent);
  const chars = [...value];
  for (const [at, char] of chars.entries()) {
    markSent();
    await raceWithTimeout(page.keyboard.type(char), timeLeft(deadline), `keyboard.type: Timeout ${budget}ms exceeded with ${at} of ${chars.length} characters typed.`);
  }
}

/**
 * Where Playwright quotes this action's text back in a failure: the value of a
 * type or a fill, and the key of a chord it does not know. Only the field the action sent is listed. The other kinds send
 * Playwright no text of the caller's, and an option label reaches it only
 * inside a locator, which it writes with line breaks escaped.
 */
function echoedText(input: ActInput): Echo[] {
  if (input.kind === "type") return [['elementHandle.type("', input.value ?? "", '")']];
  if (input.kind === "fill") return [['fill("', input.value ?? "", '")']];
  if (input.kind !== "key") return [];
  return chordKeys(keyChord(input)).map((key): Echo => ['Unknown key: "', key, '"']);
}

/**
 * A failure as its classifiers read it: Playwright's own words, one call-log
 * line each.
 *
 * The caller's text is cut from every place the message quotes it, found in the
 * message as it arrived and cut together, so one cut cannot hide another and
 * none of that text is left to read like a step. Its lines end at "\n" alone:
 * Playwright joins its log with "\n" and escapes that character in the text it
 * quotes from the page, but leaves "\r" and U+2028 in it.
 */
function failureLines(message: string, echoes: Echo[] = []): string[] {
  const cuts: Array<[start: number, end: number]> = [];
  for (const [before, text, after] of echoes) {
    if (!text) continue;
    const quoted = before + text + after;
    for (let at = message.indexOf(quoted); at >= 0; at = message.indexOf(quoted, at + 1)) {
      cuts.push([at + before.length, at + before.length + text.length]);
    }
  }
  cuts.sort((a, b) => a[0] - b[0]);
  let kept = "";
  let from = 0;
  for (const [start, end] of cuts) {
    if (start > from) kept += message.slice(from, start);
    from = Math.max(from, end);
  }
  return (kept + message.slice(from)).replace(ANSI_RE, "").split("\n");
}

/** The lines of the attempt Playwright was on when it failed. */
function lastAttempt(lines: string[]): string[] {
  let from = 0;
  lines.forEach((line, at) => { if (RETRYING_RE.test(line)) from = at + 1; });
  return lines.slice(from);
}

/**
 * Could the action have changed the page before it failed? Playwright's call
 * log names the step it reached, and only the attempt it was on counts.
 *
 * A pointer gesture Playwright retried was withheld from the page, because
 * something else (an overlay that appeared as the pointer arrived) had become
 * the hit target.
 *
 * A key press, fill or select counts once that attempt's call started and then
 * got no answer. Playwright gives up waiting on a blocked page, but a call it
 * already sent still runs once the page is free. A field that stays disabled
 * is refused and retried, and its budget ends in the pause before the next
 * attempt, so it still reads as not actionable. An answer that is an error (an
 * unknown key, a malformed date, an element that is not a select) means
 * nothing was sent.
 */
function inputMayHaveGoneOut(lines: string[]): boolean {
  const attempt = lastAttempt(lines);
  return attempt.some(line => PERFORMING_RE.test(line))
    || (TIMEOUT_RE.test(lines[0] ?? "") && attempt.some(line => STARTED_RE.test(line)));
}

/**
 * Did Playwright confirm that a pointer gesture reached its target? It writes
 * "action done" only after its own check that nothing else took the events.
 */
function gestureConfirmed(lines: string[]): boolean {
  return lastAttempt(lines).some(line => GESTURE_DONE_RE.test(line));
}

/**
 * Condense a Playwright failure on a ref that still resolves into one line: the
 * last call-log line naming the blocker (e.g. "<div></div> intercepts pointer
 * events", "element is not stable"), else the error's headline.
 */
export function actionFailureReason(lines: string[]): string {
  let reason = lines[0] ?? "";
  for (const line of lines) if (BLOCKER_RE.test(line)) reason = line;
  // Drop the call-log dash and Playwright's "locator.click: Error: " framing.
  reason = reason.trim().replace(/^-\s*/, "").replace(/^locator\.\w+:\s*/, "").replace(/^Error:\s*/, "").trim();
  if (!reason) reason = "the element did not become actionable";
  return reason.length > MAX_REASON_CHARS ? `${reason.slice(0, MAX_REASON_CHARS - 1)}…` : reason;
}

/** Error for an action that failed on a ref whose element is still current. */
export function notActionableMessage(ref: string, kind: string, reason: string): string {
  return (
    `not_actionable: ${ref} is still valid (its element is on the page), but ${kind} could not complete: ${reason}. ` +
    "Do not re-snapshot for this; wait for or remove the blocker (overlay, animation, hidden or disabled state) and retry, " +
    "or act by coordinate from a fresh browser_screenshot."
  );
}

const KEY_ALIASES: Record<string, string> = {
  Return: "Enter", Esc: "Escape", Ctrl: "Control", Cmd: "Meta", Command: "Meta",
  Option: "Alt", Del: "Delete", Spacebar: "Space", Space: " ", Win: "Meta",
};
export function normalizeKey(key: string): string {
  return key.split("+").map(part => KEY_ALIASES[part] ?? part).join("+");
}
type Modifier = "Alt" | "Control" | "ControlOrMeta" | "Meta" | "Shift";
export function normalizeModifiers(modifiers: string[] = []): Modifier[] {
  return modifiers.map(name => {
    const normalized = normalizeKey(name);
    if (!["Alt", "Control", "ControlOrMeta", "Meta", "Shift"].includes(normalized)) {
      throw new Error(`invalid_input: unknown modifier "${name}"; use Alt, Control (Ctrl), Meta (Cmd), Shift or ControlOrMeta`);
    }
    return normalized as Modifier;
  });
}
/**
 * Input errors must win over Playwright's aria-ref/actionability call log.
 * Playwright names one at the start of the failure, before any text it quotes,
 * so the same words in a typed value or an element's label are not one.
 */
export function invalidInputRemedy(message: string): string | undefined {
  if (message.startsWith("invalid_input:")) return message;
  const headline = message.split("\n", 1)[0];
  if (/^\S+: (?:Error: )?Unknown (?:key|modifier)/.test(headline)) return `invalid_input: ${actionFailureReason([headline])}. Use a Playwright key such as Enter, Escape, ArrowDown or Control+A (Return/Esc/Ctrl aliases are accepted).`;
  if (/^\S+: (?:Error: )?Malformed value/.test(headline)) return "invalid_input: malformed input value; use the native format (date YYYY-MM-DD, time HH:mm, datetime-local YYYY-MM-DDTHH:mm).";
  if (/^\S+: (?:Error: )?Element is not a <select> element/.test(headline)) return "invalid_input: select needs a native select or ARIA combobox/listbox ref; for other widgets open it and click an option.";
  return undefined;
}

export interface ActInput {
  kind: Kind;
  ref?: string;
  coordinate?: number[];
  screenshot_id?: string;
  value?: string;
  values?: string[];
  key?: string;
  modifiers?: string[];
  direction?: "up" | "down" | "left" | "right";
  amount?: number;
  to_coordinate?: number[];
  to_ref?: string;
  path?: number[];
  hold_ms?: number;
  drop_delay_ms?: number;
}

const DEFAULT_SCROLL = 400;
const NOT_EXECUTED = { outcome: "not_executed" } as const;
const DIALOG_KINDS: ReadonlySet<string> = new Set(["accept_dialog", "dismiss_dialog"]);
const REF_ONLY_KINDS: ReadonlySet<string> = new Set(["select", "check", "uncheck"]);
const MAX_URL_ECHO = 300;

/** Is this act call an answer to a pending JavaScript dialog? */
export function isDialogAnswer(input: { kind?: unknown }): boolean {
  return typeof input.kind === "string" && DIALOG_KINDS.has(input.kind);
}

/** Keys may use the active focus; other actions require one explicit target. */
export function validateTarget(input: ActInput): string | null {
  const hasRef = input.ref !== undefined;
  const hasCoord = input.coordinate !== undefined;
  if (DIALOG_KINDS.has(input.kind)) {
    return hasRef || hasCoord ? `${input.kind} takes no ref or coordinate` : null;
  }
  if (input.kind === "key" && !hasRef && !hasCoord) return null;
  if (hasRef === hasCoord) {
    return "provide exactly one of `ref` or `coordinate`";
  }
  if (hasCoord && (input.coordinate!.length !== 2)) {
    return "`coordinate` must be [x, y]";
  }
  if (hasCoord && REF_ONLY_KINDS.has(input.kind)) {
    return `\`${input.kind}\` needs a ref (take a browser_snapshot); it cannot target a coordinate`;
  }
  return null;
}

/**
 * Error for an action that may have run before it failed. Never auto-retried:
 * typing or submitting twice is worse than one extra observation.
 */
export function outcomeUnknownMessage(kind: string, lines: string[]): string {
  const reason = actionFailureReason(lines);
  return (
    `outcome_unknown: ${kind} may have taken effect before it failed (${reason}). ` +
    "Observe the page (browser_snapshot or browser_screenshot) before doing anything else; do not repeat it blindly."
  );
}

/** Errors whose message says the page or document went away mid-action. */
const PAGE_GONE_RE = /Target (?:page, context or browser )?(?:has been )?closed|Execution context was destroyed|frame was detached|navigat/i;

function staleScreenshotResult(message: string, shot: McpToolResult): McpToolResult {
  return {
    isError: true,
    content: [
      text(JSON.stringify({ ok: false, code: "stale_screenshot", error: message, ...NOT_EXECUTED })),
      ...shot.content,
    ],
  };
}

export async function runAct(session: Session, input: ActInput): Promise<McpToolResult> {
  try {
    input = { ...input, key: input.key === undefined ? undefined : normalizeKey(input.key), modifiers: normalizeModifiers(input.modifiers) };
  } catch (err) { return errorResult(String((err as Error).message), NOT_EXECUTED); }
  const targetErr = validateTarget(input);
  if (targetErr) return errorResult(`invalid_input: ${targetErr}`, NOT_EXECUTED);
  if (isDialogAnswer(input)) return answerDialog(session, input);

  const page = await session.ensurePage();
  const urlBefore = page.url();

  // Coordinates are only as good as the screenshot they were read from. One
  // the model names that is no longer the latest gets a fresh capture back.
  const usesCoordinates = input.coordinate !== undefined || input.to_coordinate !== undefined || !!input.path?.length;
  if (usesCoordinates && input.screenshot_id !== undefined && input.screenshot_id !== session.latestScreenshotId()) {
    const shot = await runScreenshot(session, {});
    return staleScreenshotResult(
      `stale_screenshot: these coordinates come from ${input.screenshot_id}, but the latest screenshot is a different one. ` +
        "A fresh viewport screenshot is attached; aim again from it.",
      shot,
    );
  }

  // A coordinate against a stale screenshot used to be a bare stale_screenshot
  // error, costing the model a screenshot call and another act. Re-validate
  // when nothing the model cannot see has changed; otherwise answer with a
  // fresh screenshot attached so the next call can aim from it directly.
  let screenshotRefreshed = false;
  if (usesCoordinates && !session.hasValidScreenshot()) {
    if (await session.tryRevalidateViewportScreenshot()) {
      screenshotRefreshed = true;
    } else {
      const shot = await runScreenshot(session, {});
      return staleScreenshotResult(
        "stale_screenshot: the page or view changed since the last browser_screenshot, so " +
          "those coordinates may no longer point where you aimed. Nothing was done. A fresh " +
          "viewport screenshot is attached; re-issue the action with coordinates from it.",
        shot,
      );
    }
  }

  // Resolve the ref up front: one we never handed out (or one from a document
  // that has since been replaced) is rejected before any page work.
  let locator: Locator | null = null;
  if (input.ref !== undefined) {
    try {
      locator = await session.resolveRef(input.ref);
    } catch (err) {
      if (err instanceof StaleRefError) {
        // Navigation may have left a passive baseline even though dispatch
        // never began. Failed actions must tear that watcher down too.
        await session.disposeWatch();
        return errorResult(err.message, NOT_EXECUTED);
      }
      throw err;
    }
  }

  const mark = session.eventMark();
  const dispatchStart = performance.now();
  // Include baseline sampling/setup in dispatch timing, then capture synchronous
  // action mutations as well as async settling.
  await session.beginWatch();

  const evidence: Record<string, unknown> = {};
  let sent = false;
  try {
    await performAction(session, page, input, locator, () => { sent = true; }, evidence);
  } catch (err) {
    // Action never settled: tear the mutation watcher down here so its
    // MutationObserver + window[WATCH_KEY] accumulator don't leak.
    session.invalidateScreenshot();
    endRequestWatch(page);
    await session.disposeWatch();
    const msg = err instanceof Error ? err.message : String(err);
    // Playwright's call log names the step it reached. Once it started to
    // change the page the outcome is no longer not_executed. An error this
    // tool raised itself has no call log, and markSent already covers it.
    const own = errorCode(msg) !== undefined;
    const log = failureLines(msg, own ? [] : echoedText(input));
    const text = log.join("\n");
    if (input.ref !== undefined && !own && inputMayHaveGoneOut(log)) sent = true;
    const remedy = invalidInputRemedy(msg);
    if (remedy && !own && input.kind === "key") sent = chordPartlyPressed(msg, keyChord(input));
    if (remedy) return sent
      ? errorResult(outcomeUnknownMessage(input.kind, [remedy]), { outcome: "unknown" })
      : errorResult(remedy, NOT_EXECUTED);
    // Already a clean stale_ref / stale_screenshot: rejected before input.
    if (msg.startsWith("stale_ref") || msg.startsWith("stale_screenshot")) return errorResult(msg, NOT_EXECUTED);
    if (sent) return errorResult(outcomeUnknownMessage(input.kind, log), { outcome: "unknown" });
    // A ref whose element has since detached times out in the actionability
    // wait. So does one whose element is still there but covered, animating or
    // otherwise not actionable, and calling THAT stale_ref sends the model to
    // re-snapshot, get the same ref back, and fail again. Ask the ref engine:
    // only a ref that no longer resolves is stale.
    if (input.ref !== undefined && locator && isRefResolutionFailure(text)) {
      // Typing is the one ref action that can stop half way: a timeout that
      // does not name a blocker may have left part of the text behind.
      if (input.kind === "type" && !log.some(line => BLOCKER_RE.test(line))) {
        return errorResult(outcomeUnknownMessage("type", log), { outcome: "unknown" });
      }
      if (!(await refStillResolves(locator))) return errorResult(new StaleRefError(input.ref).message, NOT_EXECUTED);
      return errorResult(notActionableMessage(input.ref, input.kind, actionFailureReason(log)), NOT_EXECUTED);
    }
    if (sent || PAGE_GONE_RE.test(text)) {
      return errorResult(outcomeUnknownMessage(input.kind, log), { outcome: "unknown" });
    }
    return errorResult(own ? msg : `action_failed: ${msg}`, NOT_EXECUTED);
  }

  const dispatchMs = performance.now() - dispatchStart;
  session.invalidateScreenshot();
  const settleStart = performance.now();
  // Text editing already dispatches input/change synchronously. Keep a short
  // render turn, extending it for observed timers/requests; gestures that can
  // navigate retain the 300ms quiet window.
  const settle = await session.settle(urlBefore, input.kind === "fill" || input.kind === "type" ? 100 : undefined);
  const timings = { dispatch_ms: Math.round(dispatchMs), settle_ms: Math.round(performance.now() - settleStart), snapshot_ms: 0 };
  // Only an action that changed nothing on the page leaves the last viewport
  // screenshot usable for the next coordinate.
  if (!settle.urlChanged && settle.domMutated === "none" && !settle.settleCapped && !settle.pendingRequests && !settle.pendingTimers) session.allowScreenshotRevalidation();
  const snapshotStale = settle.urlChanged || settle.domMutated === "major";
  await session.settleAdoptions();
  const events = session.eventsSince(mark);

  if (input.kind === "fill") {
    // Virtual DOMs may have replaced the acted-on element. Bound locator
    // resolution AND execution: evidence must not turn success into a 30s wait.
    // Contenteditables have no native value comparable to fill's text semantics.
    const actual = await raceWithTimeout(locator
      ? locator.evaluate(el => /^(INPUT|TEXTAREA)$/.test(el.tagName) ? (el as HTMLInputElement).value : undefined,
        undefined, { timeout: EVIDENCE_TIMEOUT_MS })
      : page.evaluate(() => {
        const el = document.activeElement;
        return el && /^(INPUT|TEXTAREA)$/.test(el.tagName) ? (el as HTMLInputElement).value : undefined;
      }), EVIDENCE_TIMEOUT_MS, "fill evidence timed out").catch(() => undefined);
    if (actual !== undefined) {
      evidence.value_matches = actual === input.value;
      if (actual !== input.value) {
        evidence.actual_value = actual.slice(0, MAX_FILL_VALUE_ECHO);
        if (actual.length > MAX_FILL_VALUE_ECHO) evidence.actual_value_truncated = true;
        evidence.note = "The resulting value differs from the requested fill; a mask/formatter may have changed it. Try kind=type to send keystrokes.";
      }
    }
  }
  if (input.kind === "scroll") {
    const point = evidence.scroll_point as number[] | undefined;
    delete evidence.scroll_point;
    const scroll = await raceWithTimeout((locator ?? page.locator("body")).evaluate(readScrollPosition,
      { direction: input.direction ?? "down", point }, { timeout: EVIDENCE_TIMEOUT_MS }),
      EVIDENCE_TIMEOUT_MS, "scroll evidence timed out").catch(() => undefined);
    if (scroll !== undefined) evidence.scroll = scroll;
  }
  const body: Record<string, unknown> = {
    ok: true,
    ...evidence,
    url_changed: settle.urlChanged,
    dom_mutated: settle.domMutated,
    snapshot_stale: snapshotStale,
    settle_capped: settle.settleCapped ?? false,
    observation_failed: settle.observationFailed ?? false,
    pending_requests: settle.pendingRequests ?? 0,
    pending_timers: settle.pendingTimers ?? 0,
    text_churn_ignored: settle.textChurnIgnored ?? false,
    timings,
  };
  if (settle.urlChanged) {
    try { body.url = page.url().slice(0, MAX_URL_ECHO); } catch { /* page closed */ }
  }
  if (events.length) body.events = events;
  if (screenshotRefreshed) body.screenshot_refreshed = true;
  // A site can withhold its captcha widget from this browser (captcha.ts).
  if (!session.pendingDialog && (await session.checkCaptcha()) === "withheld") {
    body.captcha_unavailable = true;
    body.captcha_note = CAPTCHA_AGENT_NOTE;
  }

  if (snapshotStale && !session.pendingDialog) {
    const snapshotStart = performance.now();
    const snap = await session.snapshot({ filter: "interactive" });
    timings.snapshot_ms = Math.round(performance.now() - snapshotStart);
    body.snapshot_id = snap.snapshot_id;
    return jsonResult(body, snap.text);
  }
  return jsonResult(body);
}

/**
 * accept_dialog / dismiss_dialog. Answers the pending dialog, then lets the
 * call it paused finish and returns that call's own result, marked with how
 * the dialog was answered.
 */
async function answerDialog(session: Session, input: ActInput): Promise<McpToolResult> {
  if (!session.pendingDialog) {
    return errorResult("no_dialog: no dialog is waiting for an answer. Check browser_status.", NOT_EXECUTED);
  }
  const accept = input.kind === "accept_dialog";
  const handled = accept ? "accepted" : "dismissed";
  const answer = await session.answerDialog(accept, accept ? input.value : undefined);
  const resumed = answer.resumed as McpToolResult | undefined;
  const first = resumed?.content?.[0];
  if (resumed && first && first.type === "text") {
    let body: Record<string, unknown>;
    try {
      body = JSON.parse(first.text) as Record<string, unknown>;
    } catch {
      body = { ok: !resumed.isError, result: first.text };
    }
    return {
      ...resumed,
      content: [text(JSON.stringify({ dialog_handled: handled, ...body })), ...resumed.content.slice(1)],
    };
  }
  const next = session.describeDialog();
  return jsonResult({
    ok: true,
    dialog_handled: handled,
    ...(next ? { dialog: next } : {}),
    ...(answer.finished ? {} : { note: "the call this dialog paused has not finished; observe before acting" }),
    snapshot_stale: true,
  });
}

/**
 * Run a ref pointer gesture (click, dblclick, hover). REF_ACTION_TIMEOUT_MS is
 * meant for the actionability wait, but Playwright's timeout also covers what
 * it does after sending the input: confirming the hit target and waiting for a
 * navigation the gesture scheduled. A form POST answered slowly ran out that
 * budget after the click had landed. Once the call log shows Playwright
 * confirmed the gesture, it happened, as it does for a coordinate gesture;
 * settle reports what it did. A gesture that was sent but not confirmed (the
 * page blocked before Playwright could check) is rethrown and reported as
 * outcome unknown.
 */
async function landedGesture(gesture: Promise<void>, markSent: () => void): Promise<void> {
  try {
    await gesture;
  } catch (err) {
    const lines = failureLines(err instanceof Error ? err.message : String(err));
    if (!TIMEOUT_RE.test(lines[0]) || !gestureConfirmed(lines)) throw err;
    markSent();
  }
}

async function performAction(
  session: Session,
  page: Page,
  input: ActInput,
  locator: Locator | null,
  markSent: () => void,
  evidence: Record<string, unknown>,
): Promise<void> {
  const kind = input.kind;
  if (["click", "dblclick", "hover"].includes(kind) && input.modifiers?.length) {
    const held: string[] = [];
    try {
      for (const modifier of input.modifiers) { await page.keyboard.down(modifier); held.push(modifier); }
      await performAction(session, page, { ...input, modifiers: [] }, locator, markSent, evidence);
    } finally {
      await releaseKeys(page, held);
    }
    return;
  }
  // Coordinate targets are given in the last screenshot's pixel space; map them
  // into viewport CSS pixels (scrolling into view if needed) for page.mouse.
  const point = input.coordinate
    ? await session.toViewportPoint([input.coordinate[0], input.coordinate[1]])
    : null;
  const refTimeout = { timeout: REF_ACTION_TIMEOUT_MS };

  switch (kind) {
    case "click":
      if (locator) await landedGesture(locator.click(refTimeout), markSent);
      else { markSent(); await page.mouse.click(point![0], point![1]); }
      return;

    case "dblclick":
      if (locator) await landedGesture(locator.dblclick(refTimeout), markSent);
      else { markSent(); await page.mouse.dblclick(point![0], point![1]); }
      return;

    case "hover":
      if (locator) await landedGesture(locator.hover(refTimeout), markSent);
      else { markSent(); await page.mouse.move(point![0], point![1]); }
      return;

    case "type": {
      const value = requireValue(input.value, "type");
      if (locator) await typeOnRef(page, locator, value, markSent);
      else {
        markSent();
        await page.mouse.click(point![0], point![1]);
        await page.keyboard.type(value);
      }
      return;
    }

    case "fill": {
      const value = requireValue(input.value, "fill");
      if (locator) await locator.fill(value, refTimeout);
      else {
        markSent();
        await page.mouse.click(point![0], point![1]);
        await page.keyboard.press("ControlOrMeta+A");
        await page.keyboard.type(value);
      }
      return;
    }

    case "select": {
      await selectValues(session, page, locator!, input, markSent);
      return;
    }

    case "check":
    case "uncheck":
      await locator!.setChecked(kind === "check", refTimeout);
      return;

    case "key": {
      if (!input.key) throw new Error("invalid_input: `key` requires `key`");
      const combo = keyChord(input);
      if (locator) { await pressOnRef(session, page, locator, combo, markSent); return; }
      markSent();
      try {
        await page.keyboard.press(combo);
      } catch (err) {
        // Playwright releases a chord only when the whole press succeeds.
        await releaseKeys(page, keysLeftDown(errorText(err), combo));
        throw err;
      }
      return;
    }

    case "scroll": {
      const amount = input.amount ?? DEFAULT_SCROLL;
      const [dx, dy] = scrollDelta(input.direction ?? "down", amount);
      if (locator) await locator.hover({ timeout: REF_ACTION_TIMEOUT_MS }).catch(() => undefined);
      else if (point) await page.mouse.move(point[0], point[1]);
      markSent();
      await page.mouse.wheel(dx, dy);
      if (point) evidence.scroll_point = point;
      return;
    }

    case "drag": {
      const destinations = Number(input.to_ref !== undefined) + Number(input.to_coordinate !== undefined);
      if (destinations > 1 || (!destinations && !input.path?.length)) throw new Error("invalid_input: drag requires one of to_ref, to_coordinate, or a nonempty path (path may precede one destination)");
      for (const value of [input.hold_ms, input.drop_delay_ms]) {
        if (value !== undefined && (!Number.isFinite(value) || value < 0 || value > 5000)) throw new Error("invalid_input: drag hold_ms/drop_delay_ms must be between 0 and 5000");
      }
      const points: number[][] = [];
      if (input.path && (input.path.length < 2 || input.path.length % 2)) throw new Error("invalid_input: drag path must be flat pairs [x1,y1,x2,y2,...]");
      const path = input.path ?? [];
      const waypoints = Array.from({ length: path.length / 2 }, (_, i) => [path[i * 2], path[i * 2 + 1]]);
      for (const p of [...waypoints, ...(input.to_coordinate ? [input.to_coordinate] : [])]) {
        if (p.length !== 2 || !p.every(Number.isFinite)) throw new Error("invalid_input: drag path/to_coordinate must contain finite [x, y] screenshot points");
        points.push(await session.toViewportPoint([p[0], p[1]]));
      }
      if (input.to_ref) {
        const destination = await session.resolveRef(input.to_ref);
        await destination.scrollIntoViewIfNeeded({ timeout: REF_ACTION_TIMEOUT_MS });
        const box = await destination.boundingBox({ timeout: REF_ACTION_TIMEOUT_MS });
        if (!box) throw new Error("not_actionable: drag destination has no visible bounding box");
        points.push([box.x + box.width / 2, box.y + box.height / 2]);
      }
      let from = point;
      if (!from && locator) {
        const box = await locator.boundingBox({ timeout: REF_ACTION_TIMEOUT_MS });
        if (!box) throw new Error("not_actionable: the drag source has no bounding box (hidden or detached)");
        from = [box.x + box.width / 2, box.y + box.height / 2];
      }
      if (!from) throw new Error("invalid_input: drag requires a resolvable source");
      markSent();
      await page.mouse.move(from[0], from[1]);
      await page.mouse.down();
      try {
        if (input.hold_ms) await page.waitForTimeout(input.hold_ms);
        for (const to of points) await page.mouse.move(to[0], to[1], { steps: 8 });
        if (input.drop_delay_ms) await page.waitForTimeout(input.drop_delay_ms);
      } finally { await page.mouse.up(); }
      return;
    }

    case "accept_dialog":
    case "dismiss_dialog":
      throw new Error("invalid_input: dialog answers are handled before any page action");

    default: {
      // Exhaustiveness guard.
      const _never: never = kind;
      throw new Error(`invalid_input: unsupported kind: ${_never as string}`);
    }
  }
}

/** Runs in the target's document, including iframe and open-shadow targets. */
function readScrollPosition(el: Element, args: { direction: string; point?: number[] }) {
  if (args.point) el = el.ownerDocument.elementFromPoint(args.point[0], args.point[1]) ?? el;
  const horizontal = args.direction === "left" || args.direction === "right";
  let target: Element | null = el;
  while (target && target !== el.ownerDocument.scrollingElement) {
    const style = el.ownerDocument.defaultView!.getComputedStyle(target);
    const scrollable = horizontal ? target.scrollWidth > target.clientWidth : target.scrollHeight > target.clientHeight;
    if (scrollable && /auto|scroll|overlay/.test(horizontal ? style.overflowX : style.overflowY)) break;
    target = target.parentElement ?? (target.getRootNode() as ShadowRoot).host ?? null;
  }
  target ??= el.ownerDocument.scrollingElement ?? el;
  const maxX = Math.max(0, target.scrollWidth - target.clientWidth), maxY = Math.max(0, target.scrollHeight - target.clientHeight);
  const atEnd = args.direction === "down" ? target.scrollTop >= maxY - 1 : args.direction === "up" ? target.scrollTop <= 1 : args.direction === "right" ? target.scrollLeft >= maxX - 1 : target.scrollLeft <= 1;
  return { x: target.scrollLeft, y: target.scrollTop, max_x: maxX, max_y: maxY, at_end: atEnd };
}

async function selectValues(session: Session, page: Page, locator: Locator, input: ActInput, markSent: () => void): Promise<void> {
  if (input.value !== undefined && input.values !== undefined) throw new Error("invalid_input: select takes value OR values[], not both");
  const values = input.values ?? [requireValue(input.value, "select")];
  if (!Array.isArray(values) || values.some(value => typeof value !== "string")) throw new Error("invalid_input: values must be an array of option values/labels");
  const info = await raceWithTimeout(locator.evaluate(el => ({
    native: el.tagName === "SELECT", multiple: (el as HTMLSelectElement).multiple,
    role: el.getAttribute("role"), controls: el.getAttribute("aria-controls") || el.getAttribute("aria-owns"),
    options: el.tagName === "SELECT" ? Array.from((el as HTMLSelectElement).options).map(o => ({ value: o.value, label: o.label })) : [],
  }), undefined, { timeout: REF_ACTION_TIMEOUT_MS }), REF_ACTION_TIMEOUT_MS, `Timeout ${REF_ACTION_TIMEOUT_MS}ms exceeded while checking select ref`);
  if (info.native) {
    if (!info.multiple && values.length > 1) throw new Error("invalid_input: this select is single-choice; send one value, not multiple values");
    const options = values.map(value => {
      const option = info.options.find(o => o.value === value) ?? info.options.find(o => o.label === value);
      if (!option) throw new Error(`invalid_input: option "${value}" does not exist; use an exact option value/label, or values: ["Ham", "Olives"] for a multi-select (not a comma-separated string)`);
      return option.value;
    });
    await locator.selectOption(options, { timeout: REF_ACTION_TIMEOUT_MS });
    return;
  }
  if (info.role !== "combobox" && info.role !== "listbox") throw new Error("invalid_input: select needs a native select or ARIA combobox/listbox ref; otherwise open the widget and click an option");
  if (values.length !== 1) throw new Error("invalid_input: ARIA select currently takes one label at a time; select each option separately");
  // Portal options live in the same document, not necessarily under the widget.
  const handle = await locator.elementHandle({ timeout: REF_ACTION_TIMEOUT_MS });
  const frame = await handle?.ownerFrame();
  await handle?.dispose();
  if (!frame) throw new Error("stale_ref: the select widget detached; take a new snapshot");
  const root = frame.locator("html");
  let scope = info.role === "listbox" ? locator : root;
  if (info.controls) scope = root.locator(info.controls.split(/\s+/).map(id => `[id=${JSON.stringify(id)}]`).join(","));
  if (info.role === "combobox") {
    await locator.click({ timeout: REF_ACTION_TIMEOUT_MS });
    markSent();
    if (!(await scope.getByRole("option").first().isVisible())) await pressOnRef(session, page, locator, "ArrowDown", markSent);
  }
  const option = scope.getByRole("option", { name: values[0], exact: true });
  try { await option.first().waitFor({ state: "visible", timeout: REF_ACTION_TIMEOUT_MS }); }
  catch { throw new Error(`invalid_input: no visible option labelled "${values[0]}"; inspect the open list and use its exact label`); }
  if (await option.count() !== 1) throw new Error(`invalid_input: label "${values[0]}" matches multiple options; click a specific option ref instead`);
  await option.click({ timeout: REF_ACTION_TIMEOUT_MS });
  markSent();
}

function requireValue(value: string | undefined, kind: string): string {
  if (value === undefined) throw new Error(`invalid_input: \`${kind}\` requires \`value\``);
  return value;
}

export function scrollDelta(direction: "up" | "down" | "left" | "right", amount: number): [number, number] {
  switch (direction) {
    case "up":
      return [0, -amount];
    case "down":
      return [0, amount];
    case "left":
      return [-amount, 0];
    case "right":
      return [amount, 0];
  }
}
