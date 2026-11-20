/**
 * Splits integration.browser.suite.ts into parts that Vitest runs as separate
 * files, so its two workers share the suite instead of one worker running all
 * of it (about six minutes in CI). Every part starts its own Chromium and
 * fixture server in the suite's beforeAll; each test gets a fresh page and
 * Session from its beforeEach.
 *
 * Each integration.browser.<k>.test.ts selects its part, then imports the
 * suite. Units are dealt out round-robin in registration order: an `it`, or a
 * whole `it.each` table (its rows may share state, as page isolation does).
 * A nested `describe` is registered only in parts that keep one of its units
 * (Vitest fails an empty block as "No test found"); a dry pass of its factory
 * decides that, so hooks come from here and do nothing during the dry pass.
 *
 * The deal is a pure function of registration order, so the parts together
 * register every test exactly once: `npx vitest list
 * test/integration.browser.*.test.ts` shows the split, and
 * integration-parts.test.ts checks that every part has its file.
 */
import * as vitest from "vitest";

export const PARTS = 6;

let selected: number | undefined;
let next = 0;
let dryRun = false;
let dryKept = 0;
// How many suite blocks are being collected (the root gate or a dealt describe).
let depth = 0;

function within(factory: () => void): void {
  depth++;
  try {
    factory();
  } finally {
    depth--;
  }
}

/** Call before importing the suite; part is 1-based. */
export function selectPart(part: number): void {
  if (!Number.isInteger(part) || part < 1 || part > PARTS) throw new Error(`no integration part ${part} of ${PARTS}`);
  selected = part;
}

function mine(): boolean {
  // Fail closed: importing the suite without selecting a part is a mistake.
  if (selected === undefined) throw new Error("integration.browser.suite.ts imported without selectPart()");
  const keep = next++ % PARTS === selected - 1;
  if (!dryRun) return keep;
  if (keep) dryKept++;
  return false;
}

type Body = Parameters<typeof vitest.it>[1];
type Timeout = Parameters<typeof vitest.it>[2];

function partIt(name: string, fn?: Body, timeout?: Timeout): void {
  if (mine()) vitest.it(name, fn, timeout);
}

partIt.each = <T>(table: readonly T[]) =>
  (name: string, fn: (...args: any[]) => unknown, timeout?: Timeout): void => {
    if (mine()) (vitest.it.each as any)(table)(name, fn, timeout);
  };

function partDescribe(name: string, factory: () => void): void {
  if (dryRun) return within(factory); // a block inside a block being counted
  const start = next;
  dryRun = true;
  dryKept = 0;
  try {
    within(factory);
  } finally {
    dryRun = false;
  }
  if (!dryKept) return;
  const end = next;
  // Vitest may collect the block later: replay the same units, then put the counter back.
  vitest.describe(name, () => {
    const resume = next;
    next = start;
    try {
      within(factory);
    } finally {
      if (next !== end) throw new Error(`${name}: registered ${next - start} units, counted ${end - start}`);
      next = resume;
    }
  });
}

/**
 * The suite's outer block, gated on a discoverable browser: shared by every
 * part, not dealt. Root only: a native block nested in a dealt one would
 * register during the dry pass and again on replay, so that throws.
 * The wrapped `describe` has no skipIf for the same reason.
 */
export function rootDescribeSkipIf(condition: unknown) {
  return (name: string, factory: () => void): void => {
    if (depth > 0 || dryRun) throw new Error(`${name}: rootDescribeSkipIf is only for the suite's outer block`);
    vitest.describe.skipIf(condition)(name, () => within(factory));
  };
}

function hook<F extends (...args: any[]) => unknown>(register: F): F {
  return ((...args: unknown[]) => (dryRun ? undefined : register(...args))) as F;
}

export const it = partIt;
export const describe = partDescribe;
export const beforeAll = hook(vitest.beforeAll);
export const afterAll = hook(vitest.afterAll);
export const beforeEach = hook(vitest.beforeEach);
export const afterEach = hook(vitest.afterEach);
