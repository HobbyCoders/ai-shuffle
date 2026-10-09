import { api } from './client';
import { searchSessionsPaginated, type SessionSearchResult } from './sessions';
import type { ViewType } from '$lib/stores/layout.svelte';

export interface SearchItem {
 id: string;
 label: string;
 description?: string;
 view?: ViewType;
 itemId?: string;
 session?: SessionSearchResult;
 action?: () => void;
}
export interface SearchGroup { id: string; label: string; items: SearchItem[] }
interface Named { id: string; name: string; description?: string }
interface Source { id: string; label: string; load: (query: string) => Promise<SearchItem[]> }

export const searchPages: SearchItem[] = [
 ['profile', 'Agents', 'Configure profiles, models, instructions and tools'],
 ['mcp', 'MCP servers', 'Manage configured Model Context Protocol servers'],
 ['plugins', 'Plugins', 'Installed plugins, skills and connectors'],
 ['hooks', 'Hooks', 'Event handlers and command hooks'],
 ['automations', 'Automations', 'Schedules, cron jobs and webhooks'],
 ['notes', 'Notes', 'Saved ideas and notes'],
 ['docs', 'Documentation', 'Guides and API reference'],
 ['project', 'Projects', 'Repositories and project configuration'],
 ['file-browser', 'Files', 'Browse workspace files'],
 ['prs', 'Pull requests', 'Open pull requests, reviews and checks across every project'],
 ['stats', 'Usage statistics', 'Costs, tokens and analytics'],
 ['settings', 'Settings', 'Appearance, voice, providers, default models, plugins, MCP servers, hooks, devices and security']
].map(([view, label, description]) => ({ id: `page:${view}`, view: view as ViewType, label, description }));

/**
 * The Terminal page. Listed only when the worker admits this viewer to
 * terminals (the palette checks), so it is not in `searchPages`.
 */
export const terminalPage: SearchItem = {
 id: 'page:terminal', view: 'terminal', label: 'Terminal',
 description: 'Open a shell on this worker without a chat: sign in to a CLI or finish setup'
};

export function matches(item: SearchItem, query: string): boolean {
 const text = `${item.label} ${item.description ?? ''}`.toLowerCase();
 return query.toLowerCase().split(/\s+/).every(term => text.includes(term));
}

function excerpt(text: string, query: string): string {
 const index = text.toLowerCase().indexOf(query.toLowerCase());
 const start = Math.max(0, index - 60);
 return `${start ? '…' : ''}${text.slice(start, start + 220)}${text.length > start + 220 ? '…' : ''}`;
}

// One registry per palette opening: configuration lists are reused while typing,
// then refreshed on reopen. Only public labels/descriptions enter search results.
export function createSearchSources(projectId?: string): Source[] {
 function configured(id: string, label: string, path: string, view: ViewType): Source {
  let pending: Promise<Named[]> | undefined;
  return { id, label, load: async query => {
   pending ??= api.get<Named[]>(path).catch(error => { pending = undefined; throw error; });
   return (await pending).map(row => ({ id: row.id, itemId: row.id, label: row.name, description: row.description, view }))
    .filter(item => matches(item, query)).slice(0, 10);
  } };
 }
 let mcp: Promise<SearchItem[]> | undefined;
 return [
  { id: 'sessions', label: 'Sessions', load: async query =>
   (await searchSessionsPaginated(query, { limit: 20 })).items.map(session => ({
    id: session.id, label: session.title || 'Untitled session',
    description: session.match_type === 'content' ? session.match_snippet ?? undefined : undefined, session
   })) },
  configured('agents', 'Agents', '/agents', 'profile'),
  { id: 'mcp', label: 'MCP servers', load: async query => {
   mcp ??= (async () => {
    type Server = { name: string; description?: string; scope: string };
    const response = projectId
     ? await api.get<Record<string, Server[]>>(`/projects/${encodeURIComponent(projectId)}/mcp/servers`)
     : { user: await api.get<Server[]>('/mcp/servers') };
    return ['user', 'installed', 'local', 'project'].flatMap(scope => (response[scope] ?? []).map(server => ({
     id: `${scope}:${server.name}`, label: server.name, itemId: scope === 'user' ? server.name : projectId, view: scope === 'user' ? 'mcp' as const : 'project' as const,
     description: `${scope} · ${server.description ?? 'MCP server'}`
    })));
   })().catch(error => { mcp = undefined; throw error; });
   return (await mcp).filter(item => matches(item, query)).slice(0, 10);
  } },
  configured('plugins', 'Plugins', '/plugins/installed', 'plugins'),
  { id: 'notes', label: 'Notes', load: async query => {
   const notes = await api.get<Array<{ id: string; title: string; content: string }>>(`/notes?${new URLSearchParams({ search: query, limit: '10' })}`);
   return notes.map(note => ({ id: note.id, itemId: note.id, label: note.title, description: excerpt(note.content, query), view: 'notes' }));
  } },
  { id: 'docs', label: 'Documentation', load: async query => {
   const response = await api.get<{ results: Array<{ category: string; slug: string; title: string; description: string; snippets: string[] }> }>(`/documentation/search?${new URLSearchParams({ q: query, limit: '10' })}`);
   return response.results.map(doc => ({ id: `${doc.category}/${doc.slug}`, itemId: `${doc.category}/${doc.slug}`, label: doc.title,
    description: doc.snippets[0] || doc.description, view: 'docs' }));
  } },
  configured('projects', 'Projects', '/projects', 'project'),
  configured('connectors', 'Connectors', '/connectors', 'plugins'),
  configured('automations', 'Automations', '/automations', 'automations'),
  configured('hooks', 'Hooks', '/hooks/app', 'hooks'),
  ...(projectId ? [configured('project-hooks', 'Project hooks', `/hooks/project/${encodeURIComponent(projectId)}`, 'hooks')] : [])
 ];
}
