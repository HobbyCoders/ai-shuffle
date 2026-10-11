/**
 * Reading position is an invariant: a reader who scrolled up in a session gets
 * that exact spot back after MessageArea is destroyed and remounted (opening
 * Settings or a full-page panel, then returning), and after switching to
 * another session and back.
 *
 * The per-session caches used to live in the instance script under a comment
 * claiming they were module-level. Every remount recreated them empty, so the
 * restore saw "no prior state" and pinned the reader to the bottom. Mounting
 * MessageArea pulls in the whole session graph (see MessageArea.a11y.test.ts),
 * so the save and restore paths are pinned at the source level.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');

const moduleOpen = source.indexOf('<script lang="ts" module>');
const moduleScript = source.slice(moduleOpen, source.indexOf('</script>', moduleOpen));
const instanceOpen = source.indexOf('<script lang="ts">');
const instanceScript = source.slice(instanceOpen, source.indexOf('</script>', instanceOpen));

function block(start: string, end: string): string {
	const from = instanceScript.indexOf(start);
	const to = instanceScript.indexOf(end, from);
	expect(from, start).toBeGreaterThan(-1);
	expect(to, end).toBeGreaterThan(from);
	return instanceScript.slice(from, to);
}

describe('MessageArea reading position', () => {
	it('keeps the per-session caches in the module script so they outlive a remount', () => {
		expect(moduleOpen).toBeGreaterThan(-1);
		for (const cache of ['scrollPositionCache', 'autoScrollCache']) {
			expect(moduleScript).toMatch(new RegExp(`const ${cache} = new Map<`));
			expect(instanceScript).not.toMatch(new RegExp(`(const|let) ${cache}\\b`));
		}
		expect(moduleScript).toContain('const SCROLL_CACHE_MAX = ');
	});

	it('saves the position and the follow state when the component goes away', () => {
		const destroy = block('onDestroy(() => {', '// ── Session-switch scroll save/restore');
		expect(destroy).toContain('scrollPositionCache.set(cacheKey, containerRef.scrollTop)');
		expect(destroy).toContain('autoScrollCache.set(cacheKey, shouldAutoScroll)');
	});

	it('saves the outgoing session on a switch, with the cache capped', () => {
		const sw = block('// Save scroll state for the old session', 'prevTrackedSessionId = currentId;');
		expect(sw).toContain('scrollPositionCache.set(prevId, containerRef.scrollTop)');
		expect(sw).toContain('autoScrollCache.set(prevId, shouldAutoScroll)');
		expect(sw).toContain('evictOldest(scrollPositionCache, SCROLL_CACHE_MAX)');
		expect(sw).toContain('evictOldest(autoScrollCache, SCROLL_CACHE_MAX)');
	});

	it('keeps the session-store write out of the per-frame scroll handler (#1282)', () => {
		const frame = block('function handleScroll() {', '* Release stick-to-bottom');
		// Every frame updates the non-reactive cache, never the global store.
		expect(frame).toContain('scrollPositionCache.set(cacheKey, currentScrollTop)');
		expect(frame).not.toContain('activeSession.setScrollTop(');
		expect(frame).toContain('scrollTopPersist.schedule(cacheKey, currentScrollTop)');
		// Wiring of the deferred write; its behaviour is exercised in
		// scroll-top-persist.test.ts.
		const persist = block('const scrollTopPersist = createScrollTopPersist({', 'function handleScroll() {');
		expect(persist).toContain("currentKey: () => $activeSession.sessionId ?? 'new'");
		expect(persist).toContain('writeLeft: (key, position) => sessionEphemeralCache.patchScrollTop(key, position)');
		expect(block('onDestroy(() => {', '// ── Session-switch scroll save/restore')).toContain('scrollTopPersist.destroy(cacheKey, containerRef.scrollTop)');
		// A switch files the leaving position under the LEAVING id, never into the
		// store, which already belongs to the session entered.
		const sw = block('if (currentId === prevId) return;', 'prevTrackedSessionId = currentId;');
		expect(sw).toContain('scrollTopPersist.leave(prevId, containerRef.scrollTop)');
		expect(sw).not.toContain('activeSession.setScrollTop(');
	});

	it('restores a reader who had scrolled up, over the full transcript, instead of jumping to the bottom', () => {
		const mount = block('const wasAutoScrolling = autoScrollCache.get(cacheKey);', '} else if ($activeSession.messages.length > 0) {');
		expect(mount).toContain('const userScrolledUp = wasAutoScrolling === false;');
		expect(mount).toContain('scrollPositionCache.get(cacheKey)');
		// The saved offset is measured against the whole transcript.
		expect(mount).toContain('mountedStart = 0;');
		expect(mount).toContain('shouldAutoScroll = false;');
		expect(mount).toContain('containerRef.scrollTop = storedPosition;');
	});
});
