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
 *   - any committed navigation to a NEW document invalidates all refs. A
 *     same-document navigation (history.pushState/replaceState, a hash change)
 *     keeps the document, its elements and Playwright's ref map alive, so it
 *     must not reject refs to elements that are still current. Playwright
 *     reports both kinds as `framenavigated`, so such an event only marks the
 *     refs as unverified; the next resolution compares the document identity
 *     (the main frame's CDP loaderId) recorded with the snapshot and drops
 *     every ref when it changed or cannot be read;
 *   - acting on a ref we have never handed out (or one from a prior navigation)
 *     is rejected up-front with a `stale_ref` error; acting on a ref that no
 *     longer resolves is caught at the Playwright layer and mapped the same way.
 */

export const STALE_REF_REMEDY =
  "not valid for the current page. Take a fresh browser_snapshot and use the new refs";

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
   * Identity of the document the known refs were taken from (the main frame's
   * CDP loaderId), or null when it could not be read. Same-document
   * navigations keep it; a new document always gets a new one.
   */
  private documentId: string | null = null;
  /**
   * Main-frame navigations observed since the refs were last verified against
   * the live document. Non-zero means "verify before trusting a ref".
   */
  private unverifiedNavigations = 0;

  /**
   * Record the refs from a freshly taken snapshot and advance snapshot_id.
   *
   * A full (unscoped) snapshot is the authoritative picture of the page: it
   * replaces the known set and drops any older scoped refs. A scoped snapshot
   * only refreshes the scoped set, so refs present in the latest full snapshot
   * keep resolving ("refs persist across calls within a navigation") while
   * stale refs from superseded snapshots are pruned.
   */
  registerSnapshot(refs: Iterable<string>, scoped = false, documentId: string | null = null): number {
    this.snapshotId += 1;
    if (scoped) {
      // Full-snapshot refs only survive a scoped re-read of the SAME document.
      if (documentId !== this.documentId) this.fullRefs.clear();
      this.scopedRefs = new Set(refs);
    } else {
      this.fullRefs = new Set(refs);
      this.scopedRefs.clear();
    }
    this.documentId = documentId;
    return this.snapshotId;
  }

  get currentSnapshotId(): number {
    return this.snapshotId;
  }

  /** A committed navigation destroys every element, hence every ref. */
  invalidateForNavigation(): void {
    this.fullRefs.clear();
    this.scopedRefs.clear();
    this.unverifiedNavigations = 0;
  }

  /**
   * The main frame navigated, but Playwright does not say whether to a new
   * document. Keep the refs, but require {@link verifyDocument} before any of
   * them is trusted again.
   */
  noteNavigation(): void {
    this.unverifiedNavigations += 1;
  }

  /** Has a main-frame navigation happened since the refs were last verified? */
  get needsVerification(): boolean {
    return this.unverifiedNavigations > 0;
  }

  /** Token to pass to {@link verifyDocument} (read it before the async lookup). */
  get navigationMark(): number {
    return this.unverifiedNavigations;
  }

  /**
   * Settle pending navigations against the live document identity read after
   * `mark` was taken. A different (or unreadable) document drops every ref; the
   * same document keeps them. Navigations that arrived during the lookup stay
   * pending so the next resolution checks again.
   */
  verifyDocument(liveDocumentId: string | null, mark: number): void {
    if (this.unverifiedNavigations === 0) return;
    if (liveDocumentId === null || liveDocumentId !== this.documentId) {
      this.invalidateForNavigation();
      return;
    }
    this.unverifiedNavigations = Math.max(0, this.unverifiedNavigations - mark);
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

/**
 * Validate the shape of a ref token: "e12" in the main frame, "f1e3" inside an
 * iframe (Playwright's ai snapshot numbers iframe content per frame and its
 * aria-ref engine resolves both forms).
 */
export function isRefToken(value: string): boolean {
  return /^(?:f\d+)?e\d+$/.test(value);
}
