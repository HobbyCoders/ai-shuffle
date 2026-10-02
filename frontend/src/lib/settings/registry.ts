/**
 * Settings registry: every category module and every setting entry.
 *
 * Adding a setting = one entry in `entries/<category>.ts` + one
 * `<SettingRow id="<category>.<key>">` on the page. `registry.test.ts` and the
 * coverage harness keep the two in step.
 */
import * as general from './entries/general';
import * as voice from './entries/voice';
import * as providers from './entries/providers';
import * as models from './entries/models';
import * as integrations from './entries/integrations';
import * as devices from './entries/devices';
import * as security from './entries/security';
import * as system from './entries/system';
import * as logs from './entries/logs';
import { CATEGORY_IDS, isCategoryId, type CategoryId, type CategoryModule, type SettingEntry } from './types';

export const MODULES: Record<CategoryId, CategoryModule> = {
	general,
	voice,
	providers,
	models,
	integrations,
	devices,
	security,
	system,
	logs
};

export const ENTRIES: SettingEntry[] = CATEGORY_IDS.flatMap((id) => MODULES[id].entries);

const BY_ID = new Map(ENTRIES.map((e) => [e.id, e]));

export function getEntry(id: string): SettingEntry | undefined {
	const hit = BY_ID.get(id);
	if (hit) return hit;
	// Modules swapped at runtime (tests) are still found.
	const category = id.slice(0, id.indexOf('.'));
	return isCategoryId(category) ? MODULES[category].entries.find((e) => e.id === id) : undefined;
}

/** Entries of one category, optionally narrowed to a collection item and tab. */
export function entriesFor(category: CategoryId, where: { item?: string; tab?: string } = {}): SettingEntry[] {
	return MODULES[category].entries.filter(
		(e) =>
			(where.item === undefined || e.item === where.item) &&
			(where.tab === undefined || e.tab === where.tab)
	);
}

export const HINT_MAX_WORDS = 5;

export function wordCount(text: string): number {
	return text.trim().split(/\s+/).filter(Boolean).length;
}

/** Problems with a set of entries; empty when the registry is sound. */
export function validateEntries(entries: SettingEntry[]): string[] {
	const problems: string[] = [];
	const seen = new Set<string>();
	for (const e of entries) {
		const where = e.id || '(missing id)';
		if (!e.id) problems.push(`${where}: missing id`);
		if (seen.has(e.id)) problems.push(`${where}: duplicate id`);
		seen.add(e.id);
		if (!isCategoryId(e.category)) problems.push(`${where}: unknown category "${e.category}"`);
		else if (!e.id.startsWith(`${e.category}.`)) problems.push(`${where}: id must start with "${e.category}."`);
		if (!e.label?.trim()) problems.push(`${where}: missing label`);
		else if (e.label === e.label.toUpperCase() && /[A-Z]{2}/.test(e.label) && e.label.length > 3)
			problems.push(`${where}: label is all caps`);
		if (!e.section?.trim()) problems.push(`${where}: missing section`);
		if (e.hint !== undefined && wordCount(e.hint) > HINT_MAX_WORDS)
			problems.push(`${where}: hint "${e.hint}" is over ${HINT_MAX_WORDS} words`);
		if (e.hint !== undefined && !e.hint.trim()) problems.push(`${where}: empty hint`);
		if (e.tab !== undefined && e.item === undefined) problems.push(`${where}: tab without item`);
	}
	return problems;
}

if (import.meta.env?.DEV) {
	const problems = validateEntries(ENTRIES);
	if (problems.length) console.warn('[settings] registry problems:\n' + problems.join('\n'));
}
