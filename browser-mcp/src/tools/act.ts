/**
 * browser_act — single action tool. `kind` selects the gesture; the target is
 * exactly one of `ref` (preferred) or `coordinate` [x,y] in the last
 * screenshot's pixel space. Result is terse:
 *   { ok, url_changed, dom_mutated: none|minor|major, snapshot_stale }
 * A fresh interactive snapshot is attached ONLY when the DOM changed majorly or
 * a navigation occurred (otherwise existing refs remain valid — no re-snapshot).
 */

import type { Locator, Page } from "playwright-core";
import { StaleRefError, type Session } from "../session.js";
import { jsonResult, errorResult, text, type McpToolResult } from "./result.js";
import { ACT_KINDS } from "../schemas.js";
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

/**
 * Does a Playwright error message indicate the ref locator failed to resolve to
 * an actionable element (element detached after the ref was handed out)? Such
 * failures surface as an actionability timeout naming the `aria-ref=` selector.
 * Coordinate (page.mouse) gestures do no actionability wait, so they never hang.
 */
function isRefResolutionFailure(message: string): boolean {
  return /Timeout \d+ms exceeded/i.test(message) || message.includes("aria-ref=");
}

export interface ActInput {
  kind: Kind;
  ref?: string;
  coordinate?: number[];
  value?: string;
  key?: string;
  modifiers?: string[];
  direction?: "up" | "down" | "left" | "right";
  amount?: number;
  to_coordinate?: number[];
}

const DEFAULT_SCROLL = 400;

/** Keys may use the active focus; other actions require one explicit target. */
export function validateTarget(input: ActInput): string | null {
  const hasRef = input.ref !== undefined;
  const hasCoord = input.coordinate !== undefined;
  if (input.kind === "key" && !hasRef && !hasCoord) return null;
  if (hasRef === hasCoord) {
    return "provide exactly one of `ref` or `coordinate`";
  }
  if (hasCoord && (input.coordinate!.length !== 2)) {
    return "`coordinate` must be [x, y]";
  }
  return null;
}

export async function runAct(session: Session, input: ActInput): Promise<McpToolResult> {
  const targetErr = validateTarget(input);
  if (targetErr) return errorResult(targetErr);

  const page = await session.ensurePage();
  const urlBefore = page.url();

  // A coordinate against a stale screenshot used to be a bare stale_screenshot
  // error, costing the model a screenshot call and another act (twelve such
  // errors in one audited session set). Re-validate when the view has not
  // moved; otherwise answer with a fresh screenshot attached so the next call
  // can aim from it directly.
  let screenshotRefreshed = false;
  if (input.coordinate !== undefined && !session.hasValidScreenshot()) {
    if (await session.tryRevalidateViewportScreenshot()) {
      screenshotRefreshed = true;
    } else {
      const shot = await runScreenshot(session, {});
      return {
        isError: true,
        content: [
          text(
            "stale_screenshot: the view changed since the last browser_screenshot, so " +
              "those coordinates no longer point where you aimed. A fresh viewport " +
              "screenshot is attached — re-issue the action with coordinates from it.",
          ),
          ...shot.content,
        ],
      };
    }
  }

  // Install the mutation watcher before acting so synchronous DOM changes made
  // during the action are captured, not just async settling.
  await session.beginWatch();

  try {
    await performAction(session, page, input);
  } catch (err) {
    // Action never settled: tear the mutation watcher down here so its
    // MutationObserver + window[WATCH_KEY] accumulator don't leak.
    session.invalidateScreenshot();
    await session.disposeWatch();
    const msg = err instanceof Error ? err.message : String(err);
    // Already a clean stale_ref (e.g. a ref we never handed out) — surface it.
    if (msg.startsWith("stale_ref")) return errorResult(msg);
    // A ref that resolved to a locator but whose element has since detached
    // times out in the actionability wait. Remap that (and any aria-ref
    // resolution failure) to the same stale_ref contract so the model knows to
    // re-snapshot rather than seeing a raw Playwright timeout.
    if (input.ref !== undefined && isRefResolutionFailure(msg)) {
      return errorResult(new StaleRefError(input.ref).message);
    }
    return errorResult(msg);
  }

  session.invalidateScreenshot();
  const settle = await session.settle(urlBefore);
  const snapshotStale = settle.urlChanged || settle.domMutated === "major";

  const body: Record<string, unknown> = {
    ok: true,
    url_changed: settle.urlChanged,
    dom_mutated: settle.domMutated,
    snapshot_stale: snapshotStale,
  };
  if (screenshotRefreshed) body.screenshot_refreshed = true;

  if (snapshotStale) {
    const snap = await session.snapshot({ filter: "interactive" });
    body.snapshot_id = snap.snapshot_id;
    return jsonResult(body, snap.text);
  }
  return jsonResult(body);
}

async function performAction(session: Session, page: Page, input: ActInput): Promise<void> {
  const kind = input.kind;
  const locator: Locator | null = input.ref ? session.resolveRef(input.ref) : null;
  // Coordinate targets are given in the last screenshot's pixel space; map them
  // into viewport CSS pixels (scrolling into view if needed) for page.mouse.
  const point = input.coordinate
    ? await session.toViewportPoint([input.coordinate[0], input.coordinate[1]])
    : null;
  const refTimeout = { timeout: REF_ACTION_TIMEOUT_MS };

  switch (kind) {
    case "click":
      if (locator) await locator.click(refTimeout);
      else await page.mouse.click(point![0], point![1]);
      return;

    case "dblclick":
      if (locator) await locator.dblclick(refTimeout);
      else await page.mouse.dblclick(point![0], point![1]);
      return;

    case "hover":
      if (locator) await locator.hover(refTimeout);
      else await page.mouse.move(point![0], point![1]);
      return;

    case "type": {
      const value = requireValue(input.value, "type");
      if (locator) await locator.pressSequentially(value, { timeout: typeTimeoutMs(value) });
      else {
        await page.mouse.click(point![0], point![1]);
        await page.keyboard.type(value);
      }
      return;
    }

    case "fill": {
      const value = requireValue(input.value, "fill");
      if (locator) await locator.fill(value, refTimeout);
      else {
        await page.mouse.click(point![0], point![1]);
        await page.keyboard.press("ControlOrMeta+A");
        await page.keyboard.type(value);
      }
      return;
    }

    case "select": {
      const value = requireValue(input.value, "select");
      if (!locator) throw new Error("`select` requires `ref`");
      await locator.selectOption(value, refTimeout);
      return;
    }

    case "key": {
      if (!input.key) throw new Error("`key` requires `key`");
      const combo = [...(input.modifiers ?? []), input.key].join("+");
      if (locator) await locator.press(combo, refTimeout);
      else await page.keyboard.press(combo);
      return;
    }

    case "scroll": {
      const amount = input.amount ?? DEFAULT_SCROLL;
      const [dx, dy] = scrollDelta(input.direction ?? "down", amount);
      if (locator) await locator.hover({ timeout: REF_ACTION_TIMEOUT_MS }).catch(() => undefined);
      else if (point) await page.mouse.move(point[0], point[1]);
      await page.mouse.wheel(dx, dy);
      return;
    }

    case "drag": {
      if (!input.to_coordinate || input.to_coordinate.length !== 2) {
        throw new Error("`drag` requires `to_coordinate` [x, y]");
      }
      const to = await session.toViewportPoint([input.to_coordinate[0], input.to_coordinate[1]]);
      let from = point;
      if (!from && locator) {
        const box = await locator.boundingBox({ timeout: REF_ACTION_TIMEOUT_MS });
        if (!box) throw new Error("drag source has no bounding box");
        from = [box.x + box.width / 2, box.y + box.height / 2];
      }
      if (!from) throw new Error("drag requires a resolvable source");
      await page.mouse.move(from[0], from[1]);
      await page.mouse.down();
      await page.mouse.move(to[0], to[1], { steps: 8 });
      await page.mouse.up();
      return;
    }

    default: {
      // Exhaustiveness guard.
      const _never: never = kind;
      throw new Error(`unsupported kind: ${_never as string}`);
    }
  }
}

function requireValue(value: string | undefined, kind: string): string {
  if (value === undefined) throw new Error(`\`${kind}\` requires \`value\``);
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
