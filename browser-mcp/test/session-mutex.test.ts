import { describe, it, expect } from "vitest";
import { Session } from "../src/session.js";
import { BrowserManager } from "../src/browser.js";

// runExclusive never touches the browser, so a never-connected manager is fine.
function makeSession(): Session {
  return new Session(new BrowserManager());
}

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("Session.runExclusive (per-session mutex)", () => {
  it("serializes overlapping operations (no interleaving)", async () => {
    const session = makeSession();
    const log: string[] = [];
    const task = (id: string) =>
      session.runExclusive(async () => {
        log.push(`start-${id}`);
        await delay(20);
        log.push(`end-${id}`);
      });

    // Fire both without awaiting the first — they overlap at the call site.
    await Promise.all([task("a"), task("b")]);

    // Strict A-then-B ordering proves they did not run concurrently.
    expect(log).toEqual(["start-a", "end-a", "start-b", "end-b"]);
  });

  it("preserves FIFO order across three overlapping operations", async () => {
    const session = makeSession();
    const order: number[] = [];
    await Promise.all(
      [1, 2, 3].map((n) =>
        session.runExclusive(async () => {
          await delay(5 * (4 - n)); // later tasks would finish first if unserialized
          order.push(n);
        }),
      ),
    );
    expect(order).toEqual([1, 2, 3]);
  });

  it("a rejection propagates to its caller but does not poison the queue", async () => {
    const session = makeSession();
    await expect(
      session.runExclusive(async () => {
        throw new Error("boom");
      }),
    ).rejects.toThrow("boom");

    // The next operation still runs and returns normally.
    const value = await session.runExclusive(async () => 42);
    expect(value).toBe(42);
  });
});
