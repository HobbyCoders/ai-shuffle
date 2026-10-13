/**
 * Coverage harness: the registry and the rendered rows must agree.
 *
 * For one category, every registry entry must render a
 * `[data-setting-anchor="<id>"]` where the entry says it lives (page, or
 * collection item and tab), and every anchor rendered there must be a
 * registry entry of that category. Test-only: import it from a *.test.ts.
 *
 *   import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
 *
 *   it('covers every Voice entry', async () => {
 *     await assertCategoryCoverage('voice', () => render(VoicePage).container);
 *   });
 *
 *   // Collections: called once per (item, tab) that has entries, plus once
 *   // with {} for item-less entries. Render that place and return its root.
 *   await assertCategoryCoverage('providers', async ({ item, tab }) => {
 *     settingsNav.go({ category: 'providers', item, tab });
 *     return render(ProvidersCollection).container;
 *   });
 */
import { tick } from 'svelte';
import { cleanup } from '@testing-library/svelte';
import { entriesFor, getEntry } from '../registry';
import type { CategoryId, SettingEntry } from '../types';

export interface CoverageLocation {
	item?: string;
	tab?: string;
}

export type CoverageRender = (
	where: CoverageLocation
) => HTMLElement | { container: HTMLElement } | Promise<HTMLElement | { container: HTMLElement }>;

export interface CoverageOptions {
	/**
	 * Entry ids allowed to be absent from the render, with the reason
	 * (e.g. a row that only shows on the desktop app). Keep this short.
	 */
	skip?: Record<string, string>;
	/** Extra wait after rendering, e.g. for data loads. Default: a few ticks. */
	settle?: () => Promise<void>;
	/** Unmount between locations. Default: testing-library `cleanup`. */
	cleanup?: () => void;
}

function locationKey(where: CoverageLocation): string {
	return `${where.item ?? ''}\u0000${where.tab ?? ''}`;
}

function describeLocation(category: CategoryId, where: CoverageLocation): string {
	return [category, where.item, where.tab].filter(Boolean).join(' > ');
}

async function defaultSettle(): Promise<void> {
	for (let i = 0; i < 3; i++) {
		await tick();
		await Promise.resolve();
	}
}

/** Groups a category's entries by where they render. Item-less entries form the `{}` group. */
export function coverageLocations(category: CategoryId): { where: CoverageLocation; entries: SettingEntry[] }[] {
	const groups = new Map<string, { where: CoverageLocation; entries: SettingEntry[] }>();
	groups.set(locationKey({}), { where: {}, entries: [] });
	for (const entry of entriesFor(category)) {
		const where: CoverageLocation = {
			...(entry.item ? { item: entry.item } : {}),
			...(entry.tab ? { tab: entry.tab } : {})
		};
		const key = locationKey(where);
		const group = groups.get(key) ?? { where, entries: [] };
		group.entries.push(entry);
		groups.set(key, group);
	}
	return [...groups.values()];
}

/**
 * Throws with a list of every mismatch: entries with no rendered anchor, and
 * anchors with no entry (or an entry of another category or place).
 */
export async function assertCategoryCoverage(
	category: CategoryId,
	render: CoverageRender,
	options: CoverageOptions = {}
): Promise<void> {
	const skip = options.skip ?? {};
	const settle = options.settle ?? defaultSettle;
	const unmount = options.cleanup ?? cleanup;
	const problems: string[] = [];

	for (const { where, entries } of coverageLocations(category)) {
		const result = await render(where);
		await settle();
		const root = result instanceof HTMLElement ? result : result.container;
		const anchors = Array.from(root.querySelectorAll<HTMLElement>('[data-setting-anchor]')).map(
			(el) => el.dataset.settingAnchor ?? ''
		);
		const rendered = new Set(anchors);
		const place = describeLocation(category, where);

		for (const entry of entries) {
			if (!rendered.has(entry.id) && !(entry.id in skip)) {
				problems.push(`${place}: entry "${entry.id}" has no rendered row (data-setting-anchor)`);
			}
		}
		const seen = new Set<string>();
		for (const id of anchors) {
			if (seen.has(id)) problems.push(`${place}: anchor "${id}" renders more than once`);
			seen.add(id);
			const entry = getEntry(id);
			if (!entry) {
				problems.push(`${place}: anchor "${id}" has no registry entry`);
				continue;
			}
			if (entry.category !== category) {
				problems.push(`${place}: anchor "${id}" belongs to category "${entry.category}"`);
				continue;
			}
			if (entry.item && (entry.item !== where.item || (entry.tab && entry.tab !== where.tab))) {
				problems.push(
					`${place}: anchor "${id}" is registered at ${describeLocation(category, { item: entry.item, tab: entry.tab })}`
				);
			}
		}
		unmount();
	}

	if (problems.length) {
		throw new Error(`Settings coverage for "${category}" failed:\n- ${problems.join('\n- ')}`);
	}
}
