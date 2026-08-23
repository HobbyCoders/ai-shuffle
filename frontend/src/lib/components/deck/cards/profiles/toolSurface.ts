/**
 * Pure helpers for the agent editor's tool picker (ruling T6).
 *
 * One list drives everything. `tool_selection_mode` says what the list MEANS
 * (T2): `all`/`none` carry no list at all, `allow` and `disallow` each own one.
 * Entries are exact tool names, or an `mcp__<server>__*` pattern written when a
 * whole MCP server is selected — partial selection expands back to names.
 *
 * Nothing here touches Svelte state: every function takes a list and returns a
 * new one, so the tri-state logic and the pattern-vs-names save rule are
 * testable on their own.
 */
import type {
	ToolSurface,
	ToolSurfaceGroup,
	ToolSurfaceTool,
	ToolGroupKind
} from '$lib/api/toolSurface';

export type ToolSelectionMode = 'all' | 'none' | 'allow' | 'disallow';

/** Context that decides which rows a user may actually grant. */
export interface SelectabilityContext {
	/** The profile's own computer-use switch (its group is dead while off). */
	computerUseEnabled: boolean;
	/** The signed-in user's admin flag — admin-only groups are read-only otherwise. */
	isAdmin: boolean;
}

const MODES: ToolSelectionMode[] = ['all', 'none', 'allow', 'disallow'];

/** Fixed group order (T6); MCP servers trail, alphabetically by label. */
const KIND_ORDER: ToolGroupKind[] = [
	'harness',
	'app',
	'ai',
	'browser',
	'computer_use',
	'app_config',
	'mcp'
];

export function isToolSelectionMode(value: unknown): value is ToolSelectionMode {
	return typeof value === 'string' && (MODES as string[]).includes(value);
}

/**
 * The mode a stored config is in.
 *
 * The saved mode wins outright. Rows written before `tool_selection_mode`
 * existed fall back to the legacy reading the plan spells out in T2 (`[]` →
 * none, non-empty allowed → allow, non-empty disallowed → disallow, else all)
 * so a pre-migration agent opens — and counts — as what it actually was.
 */
export function resolveToolMode(config: {
	tool_selection_mode?: unknown;
	allowed_tools?: string[] | null;
	disallowed_tools?: string[] | null;
} | null | undefined): ToolSelectionMode {
	const saved = config?.tool_selection_mode;
	if (isToolSelectionMode(saved)) return saved;
	const allowed = config?.allowed_tools;
	const disallowed = config?.disallowed_tools;
	if (Array.isArray(allowed) && allowed.length === 0) return 'none';
	if (Array.isArray(allowed) && allowed.length > 0) return 'allow';
	if (Array.isArray(disallowed) && disallowed.length > 0) return 'disallow';
	return 'all';
}

/** Groups in display order: fixed kinds first, then MCP servers by label. */
export function sortGroups(groups: ToolSurfaceGroup[]): ToolSurfaceGroup[] {
	return [...groups].sort((a, b) => {
		const ka = KIND_ORDER.indexOf(a.kind);
		const kb = KIND_ORDER.indexOf(b.kind);
		const ra = ka === -1 ? KIND_ORDER.length : ka;
		const rb = kb === -1 ? KIND_ORDER.length : kb;
		if (ra !== rb) return ra - rb;
		return a.label.localeCompare(b.label);
	});
}

/**
 * The wildcard a whole-group selection saves, or null when the group has none.
 *
 * Only user MCP servers get one (T6). The app's own bridged servers stay as
 * explicit names so a tool added to them later is not silently granted.
 */
export function groupPattern(group: ToolSurfaceGroup): string | null {
	if (group.kind !== 'mcp') return null;
	const server = group.server?.trim();
	return server ? `mcp__${server}__*` : null;
}

/** Why a row cannot be picked, or null when it can. */
export function groupBlockedReason(
	group: ToolSurfaceGroup,
	ctx: SelectabilityContext
): string | null {
	if (group.kind === 'computer_use' && !ctx.computerUseEnabled) {
		return 'Enable computer use above to grant these tools.';
	}
	if (group.available === false) {
		return 'This tool source is unavailable on this host.';
	}
	if (group.admin_only && !ctx.isAdmin) {
		return 'Admin only — you cannot grant these tools.';
	}
	return null;
}

export function toolBlockedReason(
	group: ToolSurfaceGroup,
	tool: ToolSurfaceTool,
	ctx: SelectabilityContext
): string | null {
	const groupReason = groupBlockedReason(group, ctx);
	if (groupReason) return groupReason;
	if (tool.available === false) {
		// The backend explains the shadowed builtins and the remotely-pinned
		// connector gateways; AI rows carry no reason, so the key line stands.
		const reason = tool.unavailable_reason;
		if (typeof reason === 'string' && reason.trim()) return reason;
		return 'No API key configured for this tool.';
	}
	return null;
}

/** The tools in a group a user may actually write into a list. */
export function selectableTools(
	group: ToolSurfaceGroup,
	ctx: SelectabilityContext
): ToolSurfaceTool[] {
	if (groupBlockedReason(group, ctx)) return [];
	return group.tools.filter((t) => t.available !== false);
}

/** `*` is the only wildcard the runtime's fnmatch entries ever use here. */
export function matchesToolEntry(entry: string, toolName: string): boolean {
	if (!entry) return false;
	if (entry === toolName) return true;
	if (!entry.includes('*')) return false;
	const escaped = entry.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
	return new RegExp(`^${escaped}$`).test(toolName);
}

export function listMatches(list: string[], toolName: string): boolean {
	return list.some((entry) => matchesToolEntry(entry, toolName));
}

export type GroupSelectionState = 'all' | 'some' | 'none';

/** Tri-state for a group's header checkbox, over its selectable rows only. */
export function groupSelectionState(
	group: ToolSurfaceGroup,
	list: string[],
	ctx: SelectabilityContext
): GroupSelectionState {
	const rows = selectableTools(group, ctx);
	if (rows.length === 0) return 'none';
	const selected = rows.filter((t) => listMatches(list, t.name)).length;
	if (selected === 0) return 'none';
	if (selected === rows.length) return 'all';
	return 'some';
}

/** Drop the group's pattern and every one of its tool names from a list. */
function withoutGroup(list: string[], group: ToolSurfaceGroup): string[] {
	const pattern = groupPattern(group);
	const names = new Set(group.tools.map((t) => t.name));
	return list.filter((entry) => entry !== pattern && !names.has(entry));
}

/**
 * Toggle a whole group.
 *
 * Selecting an MCP server writes its `mcp__<server>__*` pattern; every other
 * group writes the individual names. Clearing removes both shapes.
 */
export function toggleGroupSelection(
	list: string[],
	group: ToolSurfaceGroup,
	ctx: SelectabilityContext
): string[] {
	const rows = selectableTools(group, ctx);
	if (rows.length === 0) return list;
	const base = withoutGroup(list, group);
	if (groupSelectionState(group, list, ctx) === 'all') return base;
	const pattern = groupPattern(group);
	if (pattern) return [...base, pattern];
	return [...base, ...rows.map((t) => t.name)];
}

/**
 * Toggle one tool.
 *
 * A group currently held as a pattern is expanded to names first, so removing
 * a single tool from a fully-selected MCP server leaves the rest selected. If
 * the flip lands back on "everything", the names collapse to the pattern again.
 */
export function toggleToolSelection(
	list: string[],
	group: ToolSurfaceGroup,
	toolName: string,
	ctx: SelectabilityContext
): string[] {
	const rows = selectableTools(group, ctx);
	if (!rows.some((t) => t.name === toolName)) return list;

	const wasSelected = listMatches(list, toolName);
	const base = withoutGroup(list, group);
	const keep = rows
		.filter((t) => (t.name === toolName ? !wasSelected : listMatches(list, t.name)))
		.map((t) => t.name);

	const pattern = groupPattern(group);
	if (pattern && keep.length === rows.length) return [...base, pattern];
	return [...base, ...keep];
}

/** Every selectable tool across the surface — the denominator in the counts. */
export function selectableSurfaceTools(
	groups: ToolSurfaceGroup[],
	ctx: SelectabilityContext
): ToolSurfaceTool[] {
	return groups.flatMap((g) => selectableTools(g, ctx));
}

/**
 * How many tools the agent ends up with, for the header line and the card badge.
 * `allow` with an empty list resolves to none, exactly as the runtime does (T2).
 */
export function countAllowedTools(
	mode: ToolSelectionMode,
	groups: ToolSurfaceGroup[],
	list: string[],
	ctx: SelectabilityContext
): number {
	const rows = selectableSurfaceTools(groups, ctx);
	if (mode === 'none') return 0;
	if (mode === 'all') return rows.length;
	if (mode === 'allow') return rows.filter((t) => listMatches(list, t.name)).length;
	return rows.filter((t) => !listMatches(list, t.name)).length;
}

/**
 * The count badge on an agent card. Reads the stored mode and its list against
 * the live surface — no guessing at intent from list emptiness.
 */
export function profileToolCount(
	config: {
		tool_selection_mode?: unknown;
		allowed_tools?: string[] | null;
		disallowed_tools?: string[] | null;
		computer_use?: { enabled?: boolean } | null;
	} | null | undefined,
	surface: ToolSurface,
	isAdmin: boolean
): number {
	const mode = resolveToolMode(config);
	const ctx: SelectabilityContext = {
		computerUseEnabled: !!config?.computer_use?.enabled,
		isAdmin
	};
	const list =
		mode === 'allow'
			? config?.allowed_tools ?? []
			: mode === 'disallow'
				? config?.disallowed_tools ?? []
				: [];
	return countAllowedTools(mode, surface.groups, list, ctx);
}

/** Case-insensitive name/description filter for the header search box. */
export function filterGroupTools(
	group: ToolSurfaceGroup,
	query: string
): ToolSurfaceTool[] {
	const q = query.trim().toLowerCase();
	if (!q) return group.tools;
	if (group.label.toLowerCase().includes(q)) return group.tools;
	return group.tools.filter(
		(t) =>
			t.name.toLowerCase().includes(q) ||
			(t.description ?? '').toLowerCase().includes(q)
	);
}
