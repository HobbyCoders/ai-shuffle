/**
 * Wrong-project nudge: a new chat whose draft names a different registered
 * project (and not the selected one) probably belongs there. Pure string
 * matching, no model call — the composer offers a one-click project switch
 * before anything is created in the wrong place.
 */

export interface NamedProject {
	id: string;
	name: string;
}

// Generic segments that must not count as a project's distinctive name on
// their own ("server" in evrima-server-toolkit, "app" in my-app, ...).
const GENERIC = new Set([
	'app', 'apps', 'api', 'web', 'site', 'server', 'client', 'tool', 'tools', 'toolkit',
	'core', 'lib', 'docs', 'test', 'tests', 'demo', 'project', 'service', 'main', 'mobile',
	'desktop', 'backend', 'frontend', 'bot', 'data', 'config', 'scripts', 'website'
]);

function normalize(text: string): string {
	return text.toLowerCase().replace(/[_\-.]+/g, ' ').replace(/\s+/g, ' ').trim();
}

/** Ways a project can be referred to in prose: "ai-shuffle" → "ai shuffle", "aishuffle". */
export function projectAliases(name: string): string[] {
	const spaced = normalize(name);
	if (!spaced) return [];
	const aliases = new Set([spaced, spaced.replace(/ /g, '')]);
	const parts = spaced.split(' ');
	// A distinctive leading word alone ("evrima" for evrima-server-toolkit).
	if (parts.length > 1 && parts[0].length >= 5 && !GENERIC.has(parts[0])) aliases.add(parts[0]);
	return [...aliases].filter((alias) => alias.length >= 4 && !GENERIC.has(alias));
}

function mentions(text: string, alias: string): boolean {
	const escaped = alias.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	return new RegExp(`(^|[^a-z0-9])${escaped}([^a-z0-9]|$)`).test(text);
}

/**
 * The project the draft is clearly about, when it is not the selected one.
 * Returns null when the selected project is named too, when two other
 * projects are named (ambiguous), or when nothing matches.
 */
export function detectProjectMismatch(
	draft: string,
	projects: NamedProject[],
	selectedProjectId: string | null | undefined
): NamedProject | null {
	const text = normalize(draft);
	if (text.length < 8 || !projects.length) return null;
	const named = projects.filter((project) => projectAliases(project.name).some((alias) => mentions(text, alias)));
	if (!named.length || named.some((project) => project.id === selectedProjectId)) return null;
	return named.length === 1 ? named[0] : null;
}
