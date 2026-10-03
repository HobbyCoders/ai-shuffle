/**
 * MCP servers: the worker's global MCP servers (custom and built-in).
 *
 * The list is the only row that renders without data. Each server reaches
 * search through `searchSource()`, which the collection keeps current.
 */
import type { SearchItem, SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'mcp.servers',
		category: 'mcp',
		section: 'Servers',
		label: 'MCP servers',
		keywords: ['mcp', 'model context protocol', 'server', 'tools', 'stdio', 'sse', 'http', 'add server']
	}
];

let serverItems: { name: string; description?: string; builtin?: boolean }[] = [];

export function setMcpSearchItems(servers: { name: string; description?: string; builtin?: boolean }[]): void {
	serverItems = servers.map((s) => ({ name: s.name, description: s.description, builtin: s.builtin }));
}

export function searchSource(): SearchItem[] {
	return serverItems.map((s) => ({
		title: s.name,
		where: s.builtin ? 'MCP servers, Built-in' : 'MCP servers, Custom',
		keywords: ['mcp server', s.description].filter(Boolean).join(' '),
		target: { category: 'mcp' as const, item: s.name }
	}));
}
