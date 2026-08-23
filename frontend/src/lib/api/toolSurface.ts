/**
 * The live tool surface (ruling T1).
 *
 * `GET /api/v1/tools/surface` enumerates every tool a session can be given,
 * grouped by where it comes from, built from the running registries — never
 * from a hardcoded list. A server that fails to answer still appears, with an
 * empty `tools` array and an entry in `errors`, so the editor can say so
 * instead of silently dropping it.
 */
import { api } from './client';

export type ToolGroupKind =
	| 'harness'
	| 'app'
	| 'ai'
	| 'computer_use'
	| 'browser'
	| 'app_config'
	| 'mcp';

/** Coarse hazard class shown as a tag on each row; null when unclassified. */
export type ToolRisk = 'read' | 'write' | 'exec' | 'network' | null;

export interface ToolSurfaceTool {
	name: string;
	description: string;
	risk?: ToolRisk;
	/**
	 * False when no session on this install can actually receive the tool:
	 * an AI tool with no API key, a builtin the harness shadows on every
	 * native leg, or a connector gateway pair pinned to another install.
	 */
	available?: boolean;
	/** Why the row cannot be granted, when the backend knows — shown as the tooltip. */
	unavailable_reason?: string | null;
}

export interface ToolSurfaceGroup {
	id: string;
	label: string;
	kind: ToolGroupKind;
	/** MCP server name for `mcp` groups; null for the built-in groups. */
	server?: string | null;
	/** False when the whole group is unusable on this host (e.g. computer use). */
	available?: boolean;
	/** True for groups only admins may grant (the app-config server). */
	admin_only?: boolean;
	tools: ToolSurfaceTool[];
}

export interface ToolSurfaceError {
	group: string;
	message: string;
}

export interface ToolSurface {
	groups: ToolSurfaceGroup[];
	discovered_at: string;
	errors: ToolSurfaceError[];
}

export const EMPTY_TOOL_SURFACE: ToolSurface = {
	groups: [],
	discovered_at: '',
	errors: []
};

/**
 * Fetch the surface. `refresh` busts the per-server MCP enumeration cache —
 * the editor's Refresh button, not the initial load.
 */
export async function fetchToolSurface(refresh = false): Promise<ToolSurface> {
	const surface = await api.get<ToolSurface>(
		`/tools/surface${refresh ? '?refresh=true' : ''}`
	);
	return {
		groups: surface?.groups ?? [],
		discovered_at: surface?.discovered_at ?? '',
		errors: surface?.errors ?? []
	};
}
