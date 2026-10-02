/**
 * Settings data model.
 *
 * Every setting the UI renders has one `SettingEntry`. The entry carries the
 * copy (label, hint, help), the search keywords and where the row lives; the
 * page renders `<SettingRow id={entry.id}>` and reads the copy from here.
 * See docs/settings-redesign/implementation-plan.md, section 2.
 */

export const CATEGORY_IDS = [
	'general',
	'voice',
	'providers',
	'models',
	'integrations',
	'devices',
	'security',
	'system',
	'logs'
] as const;

export type CategoryId = (typeof CATEGORY_IDS)[number];
export type CategoryGroup = 'personal' | 'ai' | 'worker';
export type PageType = 'preference' | 'collection';

/** Page type per category. Collections are list + detail; the rest are one column of rows. */
export const PAGE_TYPES: Record<CategoryId, PageType> = {
	general: 'preference',
	voice: 'preference',
	providers: 'collection',
	models: 'collection',
	integrations: 'collection',
	devices: 'collection',
	security: 'preference',
	system: 'preference',
	logs: 'collection'
};

export function isCategoryId(value: unknown): value is CategoryId {
	return typeof value === 'string' && (CATEGORY_IDS as readonly string[]).includes(value);
}

/** Where a deep link lands. `anchor` is a SettingEntry id; `open` expands that row. */
export interface SettingsTarget {
	category: CategoryId;
	item?: string;
	tab?: string;
	anchor?: string;
	open?: string;
}

export interface SettingEntry {
	/** `<category>.<key>`, unique; also the row's `data-setting-anchor`. */
	id: string;
	category: CategoryId;
	/** Group title on the page (or tab) and the second half of the search "where". */
	section: string;
	label: string;
	/** At most 5 words (test-enforced). */
	hint?: string;
	/** Text for the "?" tooltip. Use sparingly. */
	help?: string;
	/** Search synonyms. */
	keywords?: string[];
	/** Location inside a collection. */
	item?: string;
	tab?: string;
	/** `desktop-app` rows are left out of search outside the desktop app. */
	platform?: 'all' | 'desktop-app';
	/** Value that counts as unmodified; drives the modified edge and reset. */
	default?: unknown;
}

export interface SearchItem {
	title: string;
	/** "Category, Section" style breadcrumb shown under the title. */
	where: string;
	keywords?: string;
	target: SettingsTarget;
}

export type AttentionTone = 'warn' | 'err';
export interface Attention {
	count: number;
	tone: AttentionTone;
}

export interface CategoryModule {
	entries: SettingEntry[];
	/** Dynamic search items (providers, devices, API users...) read from live stores. */
	searchSource?: () => SearchItem[];
	/** Index badge. Return null when nothing needs attention. */
	attention?: () => Attention | null;
}

/** Phone-stack step. On wide layouts every pane shows and this only records depth. */
export type SettingsView = 'index' | 'page' | 'detail';

export type StatusTone = 'ok' | 'warn' | 'err' | 'off' | 'standby' | 'busy';
