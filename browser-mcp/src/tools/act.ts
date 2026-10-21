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

import type { Locator, Page } from "playwright-core";
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
/** Call-log lines that name why an action could not proceed. */
const BLOCKER_RE = /intercepts pointer events|element is not |element is outside of the viewport|not attached|is disabled/i;
const MAX_REASON_CHARS = 240;

/**
 * Condense a Playwright failure on a ref that still resolves into one line: the
 * last call-log line naming the blocker (e.g. "<div></div> intercepts pointer
 * events", "element is not stable"), else the error's headline.
 */
export function actionFailureReason(message: string): string {
  const lines = message.replace(ANSI_RE, "").split("\n").map((line) => line.trim().replace(/^-\s*/, ""));
  let reason = "";
  for (const line of lines) if (BLOCKER_RE.test(line)) reason = line;
  if (!reason) reason = lines[0] ?? "";
  // Drop Playwright's "locator.click: Error: " framing.
  reason = reason.replace(/^locator\.\w+:\s*/, "").replace(/^Error:\s*/, "").trim();
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
/** Input errors must win over Playwright's aria-ref/actionability call log. */
export function invalidInputRemedy(message: string): string | undefined {
  if (message.startsWith("invalid_input:")) return message;
  if (/Unknown key|Unknown modifier/i.test(message)) return `invalid_input: ${actionFailureReason(message)}. Use a Playwright key such as Enter, Escape, ArrowDown or Control+A (Return/Esc/Ctrl aliases are accepted).`;
  if (/Malformed value/i.test(message)) return "invalid_input: malformed input value; use the native format (date YYYY-MM-DD, time HH:mm, datetime-local YYYY-MM-DDTHH:mm).";
  if (/not (?:a |an )?(?:<select>|select element)/i.test(message)) return "invalid_input: select needs a native select or ARIA combobox/listbox ref; for other widgets open it and click an option.";
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
export function outcomeUnknownMessage(kind: string, detail: string): string {
  const reason = actionFailureReason(detail);
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
      if (err instanceof StaleRefError) return errorResult(err.message, NOT_EXECUTED);
      throw err;
    }
  }

  const mark = session.eventMark();
  // Install the mutation watcher before acting so synchronous DOM changes made
  // during the action are captured, not just async settling.
  await session.beginWatch();

  const dispatchStart = performance.now();
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
    const remedy = invalidInputRemedy(msg);
    if (remedy) return sent
      ? errorResult(outcomeUnknownMessage(input.kind, remedy), { outcome: "unknown" })
      : errorResult(remedy, NOT_EXECUTED);
    // Already a clean stale_ref / stale_screenshot: rejected before input.
    if (msg.startsWith("stale_ref") || msg.startsWith("stale_screenshot")) return errorResult(msg, NOT_EXECUTED);
    if (sent) return errorResult(outcomeUnknownMessage(input.kind, msg), { outcome: "unknown" });
    // A ref whose element has since detached times out in the actionability
    // wait. So does one whose element is still there but covered, animating or
    // otherwise not actionable, and calling THAT stale_ref sends the model to
    // re-snapshot, get the same ref back, and fail again. Ask the ref engine:
    // only a ref that no longer resolves is stale.
    if (input.ref !== undefined && locator && isRefResolutionFailure(msg)) {
      // Typing is the one ref action that can stop half way: a timeout that
      // does not name a blocker may have left part of the text behind.
      if (input.kind === "type" && !BLOCKER_RE.test(msg.replace(ANSI_RE, ""))) {
        return errorResult(outcomeUnknownMessage("type", msg), { outcome: "unknown" });
      }
      if (!(await refStillResolves(locator))) return errorResult(new StaleRefError(input.ref).message, NOT_EXECUTED);
      return errorResult(notActionableMessage(input.ref, input.kind, actionFailureReason(msg)), NOT_EXECUTED);
    }
    if (sent || PAGE_GONE_RE.test(msg)) {
      return errorResult(outcomeUnknownMessage(input.kind, msg), { outcome: "unknown" });
    }
    return errorResult(errorCode(msg) ? msg : `action_failed: ${msg}`, NOT_EXECUTED);
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
      for (const modifier of held.reverse()) await page.keyboard.up(modifier);
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
      if (locator) await locator.click(refTimeout);
      else { markSent(); await page.mouse.click(point![0], point![1]); }
      return;

    case "dblclick":
      if (locator) await locator.dblclick(refTimeout);
      else { markSent(); await page.mouse.dblclick(point![0], point![1]); }
      return;

    case "hover":
      if (locator) await locator.hover(refTimeout);
      else { markSent(); await page.mouse.move(point![0], point![1]); }
      return;

    case "type": {
      const value = requireValue(input.value, "type");
      if (locator) await locator.pressSequentially(value, { timeout: typeTimeoutMs(value) });
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
      await selectValues(locator!, input, markSent);
      return;
    }

    case "check":
    case "uncheck":
      await locator!.setChecked(kind === "check", refTimeout);
      return;

    case "key": {
      if (!input.key) throw new Error("invalid_input: `key` requires `key`");
      const combo = [...(input.modifiers ?? []), input.key].join("+");
      if (locator) await locator.press(combo, refTimeout);
      else { markSent(); await page.keyboard.press(combo); }
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

async function selectValues(locator: Locator, input: ActInput, markSent: () => void): Promise<void> {
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
    if (!(await scope.getByRole("option").first().isVisible())) await locator.press("ArrowDown", { timeout: REF_ACTION_TIMEOUT_MS });
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
