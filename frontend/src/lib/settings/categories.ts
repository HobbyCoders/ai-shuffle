/**
 * The Settings categories, in index order, grouped Personal / AI / This worker.
 */
import {
	SlidersHorizontal,
	Archive,
	AudioLines,
	KeyRound,
	Sparkles,
	Plug,
	Puzzle,
	Cable,
	Webhook,
	MonitorSmartphone,
	Shield,
	Server,
	ScrollText
} from 'lucide-svelte';
import { PAGE_TYPES, type CategoryGroup, type CategoryId, type PageType } from './types';

export type SettingsIcon = typeof SlidersHorizontal;

export interface CategoryDef {
	id: CategoryId;
	label: string;
	icon: SettingsIcon;
	group: CategoryGroup;
	type: PageType;
}

type CategoryRow = Omit<CategoryDef, 'type'>;

export const CATEGORY_GROUPS: { id: CategoryGroup; label: string }[] = [
	{ id: 'personal', label: 'Personal' },
	{ id: 'ai', label: 'AI' },
	{ id: 'worker', label: 'This worker' }
];

const ROWS: CategoryRow[] = [
	{ id: 'general', label: 'General', icon: SlidersHorizontal, group: 'personal' },
	{ id: 'archived', label: 'Archived chats', icon: Archive, group: 'personal' },
	{ id: 'voice', label: 'Voice', icon: AudioLines, group: 'personal' },
	{ id: 'providers', label: 'Providers', icon: KeyRound, group: 'ai' },
	{ id: 'models', label: 'Default models', icon: Sparkles, group: 'ai' },
	{ id: 'integrations', label: 'Integrations', icon: Plug, group: 'ai' },
	{ id: 'plugins', label: 'Plugins', icon: Puzzle, group: 'ai' },
	{ id: 'mcp', label: 'MCP servers', icon: Cable, group: 'ai' },
	{ id: 'hooks', label: 'Hooks', icon: Webhook, group: 'ai' },
	{ id: 'devices', label: 'Devices', icon: MonitorSmartphone, group: 'worker' },
	{ id: 'security', label: 'Security', icon: Shield, group: 'worker' },
	{ id: 'system', label: 'System', icon: Server, group: 'worker' },
	{ id: 'logs', label: 'Logs', icon: ScrollText, group: 'worker' }
];

export const CATEGORIES: CategoryDef[] = ROWS.map((c) => ({ ...c, type: PAGE_TYPES[c.id] }));

const BY_ID = new Map(CATEGORIES.map((c) => [c.id, c]));

export function categoryById(id: CategoryId): CategoryDef {
	return BY_ID.get(id)!;
}

export function categoryGroupLabel(group: CategoryGroup): string {
	return CATEGORY_GROUPS.find((g) => g.id === group)?.label ?? '';
}
