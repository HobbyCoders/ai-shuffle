/**
 * MCP Server Management Store -- Re-export shim.
 * Implementation lives in mcp.svelte.ts (Svelte 5 runes).
 */
export {
	mcp,
	globalMcpServers,
	projectMcpServers,
	allProjectMcpServers,
	mcpTools,
	mcpToolsByServer,
	mcpStats,
	mcpLoading,
	mcpError,
	totalMcpServerCount,
	mcpDisplayStats,
} from './mcp.svelte';

export type { McpState } from './mcp.svelte';
