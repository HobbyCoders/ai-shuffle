/**
 * Ref lifecycle & resolution.
 *
 * Refs (e1, e2, …) are assigned by Playwright's aria snapshot (ai mode) and
 * resolved back to elements through the built-in `aria-ref=<ref>` selector
 * engine — we deliberately do NOT build a parallel DOM-walking resolver.
 *
 * Policy:
 *   - refs persist across snapshot calls within a single navigation (Playwright
 *     caches `element._ariaRef`, so a surviving element keeps its ref);
 *   - every snapshot response carries a monotonic `snapshot_id`;
 *   - any committed navigation invalidates all refs;
 *   - acting on a ref we have never handed out (or one from a prior navigation)
 *     is rejected up-front with a `stale_ref` error; acting on a ref that no
 *     longer resolves is caught at the Playwright layer and mapped the same way.
 */

export const STALE_REF_REMEDY =
  "ref not valid for the current page — take a fresh browser_snapshot and use the new refs";

export class RefRegistry {
  private snapshotId = 0;
  /**
   * Refs from the most recent FULL (unscoped) snapshot. A new full snapshot
   * replaces this set — older refs are pruned so an SPA that re-snapshots many
   * times within one navigation cannot grow this without bound.
   */
  private fullRefs = new Set<string>();
  /**
   * Refs from the most recent SCOPED snapshot, unioned onto the full set at
   * resolution time so a ref valid in a narrowed subtree is not falsely
   * rejected. Cleared whenever a new full snapshot lands.
   */
  private scopedRefs = new Set<string>();

  /**
   * Record the refs from a freshly taken snapshot and advance snapshot_id.
   *
   * A full (unscoped) snapshot is the authoritative picture of the page: it
   * replaces the known set and drops any older scoped refs. A scoped snapshot
   * only refreshes the scoped set, so refs present in the latest full snapshot
   * keep resolving ("refs persist across calls within a navigation") while
   * stale refs from superseded snapshots are pruned.
   */
  registerSnapshot(refs: Iterable<string>, scoped = false): number {
    this.snapshotId += 1;
    if (scoped) {
      this.scopedRefs = new Set(refs);
    } else {
      this.fullRefs = new Set(refs);
      this.scopedRefs.clear();
    }
    return this.snapshotId;
  }

  get currentSnapshotId(): number {
    return this.snapshotId;
  }

  /** A committed navigation destroys every element, hence every ref. */
  invalidateForNavigation(): void {
    this.fullRefs.clear();
    this.scopedRefs.clear();
  }

  isKnown(ref: string): boolean {
    return this.fullRefs.has(ref) || this.scopedRefs.has(ref);
  }

  knownCount(): number {
    let n = this.fullRefs.size;
    for (const ref of this.scopedRefs) if (!this.fullRefs.has(ref)) n += 1;
    return n;
  }

  /** Selector understood by the aria-ref selector engine. */
  static selectorFor(ref: string): string {
    return `aria-ref=${ref}`;
  }
}

/** Validate the shape of a ref token (e.g. "e12"). */
export function isRefToken(value: string): boolean {
  return /^e\d+$/.test(value);
}
