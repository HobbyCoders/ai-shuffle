/**
 * The Settings categories, in index order, in five groups (CATEGORY_GROUPS).
 */
import {
	BotMessageSquare,
	SlidersHorizontal,
	SlidersVertical,
	Archive,
	AudioLines,
	KeyRound,
	Sparkles,
	Plug,
	Puzzle,
	Cable,
	Webhook,
	MonitorSmartphone,
	Cloud,
	Shield,
	Vault,
	Server,
	ScrollText,
	Gauge,
	Brain
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

/**
 * Personal: how the app looks and listens to you. Models: which models run
 * and what they may spend. Agents and tools: how agents behave and what they
 * can reach. Devices and access: who and what can reach this worker.
 * Maintenance: keeping the worker itself running.
 */
export const CATEGORY_GROUPS: { id: CategoryGroup; label: string }[] = [
	{ id: 'personal', label: 'Personal' },
	{ id: 'models', label: 'Models' },
	{ id: 'agents', label: 'Agents and tools' },
	{ id: 'access', label: 'Devices and access' },
	{ id: 'maintenance', label: 'Maintenance' }
];

const ROWS: CategoryRow[] = [
	{ id: 'general', label: 'General', icon: SlidersHorizontal, group: 'personal' },
	{ id: 'voice', label: 'Voice', icon: AudioLines, group: 'personal' },
	{ id: 'providers', label: 'Providers', icon: KeyRound, group: 'models' },
	{ id: 'models', label: 'Default models', icon: Sparkles, group: 'models' },
	{ id: 'tuning', label: 'Model tuning', icon: SlidersVertical, group: 'models' },
	{ id: 'usage', label: 'Usage', icon: Gauge, group: 'models' },
	// Not "Agents": that name and the Bot icon belong to the sidebar's Agents
	// area (profiles). This page is the behavior every agent shares.
	{ id: 'agents', label: 'Agent behavior', icon: BotMessageSquare, group: 'agents' },
	{ id: 'memory', label: 'Memory', icon: Brain, group: 'agents' },
	{ id: 'plugins', label: 'Plugins', icon: Puzzle, group: 'agents' },
	{ id: 'mcp', label: 'MCP servers', icon: Cable, group: 'agents' },
	{ id: 'hooks', label: 'Hooks', icon: Webhook, group: 'agents' },
	{ id: 'integrations', label: 'Integrations', icon: Plug, group: 'agents' },
	{ id: 'devices', label: 'Devices', icon: MonitorSmartphone, group: 'access' },
	{ id: 'cloud', label: 'Cloud', icon: Cloud, group: 'access' },
	{ id: 'security', label: 'Security', icon: Shield, group: 'access' },
	{ id: 'vault', label: 'Vault', icon: Vault, group: 'access' },
	{ id: 'system', label: 'System', icon: Server, group: 'maintenance' },
	{ id: 'logs', label: 'Logs', icon: ScrollText, group: 'maintenance' }
];

export const CATEGORIES: CategoryDef[] = ROWS.map((c) => ({ ...c, type: PAGE_TYPES[c.id] }));

const BY_ID = new Map(CATEGORIES.map((c) => [c.id, c]));

export function categoryById(id: CategoryId): CategoryDef {
	return BY_ID.get(id)!;
}

export function categoryGroupLabel(group: CategoryGroup): string {
	return CATEGORY_GROUPS.find((g) => g.id === group)?.label ?? '';
}
