/**
 * Per-page ring buffers for console messages and network responses.
 *
 * Each page (tab) owns one {@link ConsoleBuffer} and one {@link NetworkBuffer};
 * the Session installs `page.on('console')` / `page.on('response')` listeners
 * once per page (at acquisition) that push into them. Both are bounded rings so
 * a chatty page cannot grow memory without bound. The buffers are pure and
 * unit-tested in isolation; the browser wiring lives in session.ts.
 */

import type { Response } from "playwright-core";

/** Console messages retained per page before the oldest are dropped. */
export const CONSOLE_BUFFER_CAP = 500;
/** Network responses retained per page before the oldest are dropped. */
export const NETWORK_BUFFER_CAP = 500;

/** A minimal bounded ring: newest at the end, oldest dropped past `cap`. */
export class RingBuffer<T> {
  private items: T[] = [];
  constructor(private readonly cap: number) {}

  push(item: T): void {
    this.items.push(item);
    if (this.items.length > this.cap) {
      this.items.splice(0, this.items.length - this.cap);
    }
  }

  clear(): void {
    this.items = [];
  }

  /** Oldest → newest. */
  all(): readonly T[] {
    return this.items;
  }

  get length(): number {
    return this.items.length;
  }
}

/**
 * Per-entry char cap for console text (and its location string). The ring
 * bounds the COUNT of entries; this bounds the SIZE of any single one so one
 * `console.log` of a megabyte-long string cannot be retained whole.
 */
export const CONSOLE_ENTRY_MAX_CHARS = 2000;

export interface ConsoleEntry {
  /** Console level (log, info, warn, error, debug, …). */
  level: string;
  text: string;
  /** Short "url:line:col" of the emitting call site, or "". */
  location: string;
}

/** Build a {@link ConsoleEntry}, truncating text/location to the per-entry cap. */
export function cappedConsoleEntry(level: string, text: string, location: string): ConsoleEntry {
  return {
    level,
    text: text.length > CONSOLE_ENTRY_MAX_CHARS ? text.slice(0, CONSOLE_ENTRY_MAX_CHARS) : text,
    location:
      location.length > CONSOLE_ENTRY_MAX_CHARS
        ? location.slice(0, CONSOLE_ENTRY_MAX_CHARS)
        : location,
  };
}

export type ConsoleBuffer = RingBuffer<ConsoleEntry>;

export function newConsoleBuffer(): ConsoleBuffer {
  return new RingBuffer<ConsoleEntry>(CONSOLE_BUFFER_CAP);
}

/** A recorded network response. `response` is retained for lazy body fetch. */
export interface NetworkEntry {
  id: string;
  method: string;
  url: string;
  status: number;
  resourceType: string;
  /** Live Playwright handle — `.text()` at ask-time can fail post-navigation. */
  response: Response;
}

/** Terse view of a {@link NetworkEntry} (no live handle) for listing. */
export interface NetworkListItem {
  id: string;
  method: string;
  url: string;
  status: number;
  resourceType: string;
}

export function toListItem(e: NetworkEntry): NetworkListItem {
  return {
    id: e.id,
    method: e.method,
    url: e.url,
    status: e.status,
    resourceType: e.resourceType,
  };
}

/**
 * Ring buffer of network responses that also assigns each a stable, per-page
 * id (`req1`, `req2`, …) used by browser_network's two-phase list/body flow.
 */
export class NetworkBuffer {
  private readonly ring = new RingBuffer<NetworkEntry>(NETWORK_BUFFER_CAP);
  private seq = 0;

  record(fields: Omit<NetworkEntry, "id">): NetworkEntry {
    this.seq += 1;
    const entry: NetworkEntry = { id: `req${this.seq}`, ...fields };
    this.ring.push(entry);
    return entry;
  }

  clear(): void {
    this.ring.clear();
  }

  all(): readonly NetworkEntry[] {
    return this.ring.all();
  }

  find(id: string): NetworkEntry | undefined {
    return this.ring.all().find((e) => e.id === id);
  }

  get length(): number {
    return this.ring.length;
  }
}
