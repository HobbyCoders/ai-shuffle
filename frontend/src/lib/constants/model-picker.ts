/**
 * Shared catalog shape for every model picker surface (profile/agent editor,
 * chat composer dock, mobile controls sheet). Built from the live model
 * registry so the three surfaces can never disagree about naming or grouping.
 *
 * Groups are keyed by provider ID (native Claude gets a sentinel) — two rows
 * sharing a brand (both OpenAI rows display as "OpenAI") stay separate groups;
 * the auth-kind subtext is what tells them apart.
 */
import {
	getModels,
	getProviders,
	encodeProviderModel,
	getProviderDisplay,
	getClaudeDisplay,
	getClaudeAuth,
	NATIVE_PROVIDER_KEY,
	type ModelFamily
} from './models.svelte';
import { providerGlyphKind, type ProviderGlyphKind } from '$lib/components/settings/ProviderGlyph.svelte';

/** Stable bucket key for the native Claude catalog. */
const NATIVE_GROUP_KEY = '__claude__';

export interface PickerGroup {
	/** Stable bucket identity (provider id, or the native sentinel). */
	key: string;
	/** Brand name shown in the rail tooltip and row subline. */
	name: string;
	/** Muted auth-kind qualifier ("api" / "prolite" / …). */
	subtext: string | null;
	/** Which brand glyph the rail/rows draw. */
	glyph: ProviderGlyphKind;
	/** Whether this provider authenticates with an API credential. */
	isApi: boolean;
}

export interface PickerOption {
	/** Composite value emitted on select (native alias or `prov:` composite). */
	value: string;
	/** Primary row label. */
	label: string;
	/** Raw model id / blurb shown as the search haystack and row tooltip. */
	description: string;
	groupKey: string;
	/** Convenience copies of the owning group's display — kept in sync by the builder. */
	group: string;
	groupSubtext: string | null;
}

export interface PickerCatalog {
	groups: PickerGroup[];
	options: PickerOption[];
}

const MODEL_FAMILY_BLURB: Record<ModelFamily, string> = {
	haiku: 'Fastest, lightest',
	sonnet: 'Balanced speed and depth',
	opus: 'Deep reasoning',
	fable: 'Most capable'
};

export interface BuildCatalogOptions {
	/** Suppress the native Claude group. */
	providerModelsOnly?: boolean;
	/** Limit to one provider. Use NATIVE_PROVIDER_KEY for native Claude. */
	providerScope?: string;
	/** Use the family blurb ("Deep reasoning") as the native rows' description instead of the alias. */
	familyBlurbs?: boolean;
}

/** Build the grouped catalog from the live registry. Call inside `$derived` for reactivity. */
export function buildModelPickerCatalog(opts: BuildCatalogOptions = {}): PickerCatalog {
	const groups: PickerGroup[] = [];
	const options: PickerOption[] = [];
	const { providerModelsOnly = false, providerScope, familyBlurbs = false } = opts;

	// Bundled Claude metadata also labels historical sessions. It is not proof
	// that this worker has an account or API key that can run those models.
	const claudeAuth = getClaudeAuth();
	if (claudeAuth.auth_kind && !providerModelsOnly && (!providerScope || providerScope === NATIVE_PROVIDER_KEY)) {
		const claude = getClaudeDisplay({
			planType: claudeAuth.plan_type,
			authKind: claudeAuth.auth_kind
		});
		const models = getModels();
		if (models.length > 0) {
			groups.push({
				key: NATIVE_GROUP_KEY,
				name: claude.name,
				subtext: claude.subtext,
				glyph: 'claude',
				isApi: claudeAuth.auth_kind === 'api'
			});
		}
		for (const m of models) {
			options.push({
				value: m.value,
				label: m.label,
				description: familyBlurbs ? (MODEL_FAMILY_BLURB[m.family] ?? m.value) : m.value,
				groupKey: NATIVE_GROUP_KEY,
				group: claude.name,
				groupSubtext: claude.subtext
			});
		}
	}

	for (const p of getProviders()) {
		if (providerScope && providerScope !== p.id) continue;
		const display = getProviderDisplay(p);
		groups.push({
			key: p.id,
			name: display.name,
			subtext: display.subtext,
			glyph: providerGlyphKind(p.provider_type),
			isApi: p.auth_kind === 'api'
		});
		for (const pm of p.models) {
			options.push({
				value: encodeProviderModel(p.id, pm.id),
				label: pm.label,
				description: pm.id,
				groupKey: p.id,
				group: display.name,
				groupSubtext: display.subtext
			});
		}
	}

	return { groups, options };
}

/** Case-insensitive substring match over label, description and group name. */
export function matchesPickerQuery(option: PickerOption, query: string): boolean {
	const q = query.trim().toLowerCase();
	if (!q) return true;
	return (
		option.label.toLowerCase().includes(q) ||
		option.description.toLowerCase().includes(q) ||
		option.group.toLowerCase().includes(q) ||
		(option.groupSubtext ?? '').toLowerCase().includes(q)
	);
}
