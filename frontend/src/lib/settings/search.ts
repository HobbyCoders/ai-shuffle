/**
 * Settings search: one flat index of categories, registry entries and each
 * category's dynamic `searchSource()` items.
 *
 * A result matches when every query word appears somewhere in its title,
 * "where" breadcrumb or keywords. Titles that start with the first word rank
 * first, then titles that contain it; duplicates (same title and where) drop.
 */
import { desktop } from '$lib/services/desktop';
import { CATEGORIES, categoryById, categoryGroupLabel } from './categories';
import { MODULES } from './registry';
import { CATEGORY_IDS, type SearchItem, type SettingEntry } from './types';

export const MAX_RESULTS = 12;

function entryItem(e: SettingEntry): SearchItem {
	const cat = categoryById(e.category);
	const where = [cat.label, e.section].filter(Boolean).join(', ');
	return {
		title: e.label,
		where,
		keywords: [...(e.keywords ?? []), e.hint ?? ''].join(' ').trim() || undefined,
		target: {
			category: e.category,
			...(e.item ? { item: e.item } : {}),
			...(e.tab ? { tab: e.tab } : {}),
			anchor: e.id
		}
	};
}

export interface BuildIndexOptions {
	/** Defaults to whether this is the desktop app. */
	desktopApp?: boolean;
}

export function buildIndex(options: BuildIndexOptions = {}): SearchItem[] {
	const desktopApp = options.desktopApp ?? desktop !== null;
	const index: SearchItem[] = CATEGORIES.map((c) => ({
		title: c.label,
		where: categoryGroupLabel(c.group),
		target: { category: c.id }
	}));
	for (const id of CATEGORY_IDS) {
		const mod = MODULES[id];
		for (const e of mod.entries) {
			if (e.platform === 'desktop-app' && !desktopApp) continue;
			index.push(entryItem(e));
		}
		if (mod.searchSource) {
			try {
				index.push(...mod.searchSource());
			} catch (err) {
				console.warn(`[settings] search source for ${id} failed`, err);
			}
		}
	}
	return index;
}

function haystack(item: SearchItem): string {
	return `${item.title} ${item.where} ${item.keywords ?? ''}`.toLowerCase();
}

export function query(q: string, index: SearchItem[] = buildIndex()): SearchItem[] {
	const words = q.toLowerCase().trim().split(/\s+/).filter(Boolean);
	if (!words.length) return [];
	const first = words[0];
	const rank = (item: SearchItem) => {
		const title = item.title.toLowerCase();
		if (title.startsWith(first)) return 0;
		if (title.split(/\s+/).some((w) => w.startsWith(first))) return 1;
		if (title.includes(first)) return 2;
		return 3;
	};
	const seen = new Set<string>();
	return index
		.filter((item) => {
			const text = haystack(item);
			return words.every((w) => text.includes(w));
		})
		.map((item, order) => ({ item, order, rank: rank(item) }))
		.sort((a, b) => a.rank - b.rank || a.order - b.order)
		.map((r) => r.item)
		.filter((item) => {
			const key = `${item.title}\u0000${item.where}`;
			if (seen.has(key)) return false;
			seen.add(key);
			return true;
		})
		.slice(0, MAX_RESULTS);
}
