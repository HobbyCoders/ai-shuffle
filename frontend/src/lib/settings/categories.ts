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
	Brain,
	GraduationCap
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
 * and what they may spend. Agents and tools: what every agent is told and the
 * tools it can use. Connections and security: what this worker is linked to
 * (other devices, the cloud account, GitHub and API keys) and what may reach
 * or control it (sign-in, agent computer access, saved sign-ins). System and
 * data: the worker itself, its data, updates and logs.
 *
 * Borderline calls: Integrations (GitHub, API keys) goes with Connections,
 * because both halves link this worker to something outside it. Vault goes with
 * Security, because it holds secrets. Import goes beside Export in System >
 * Your data, because it moves data in; it doesn't install tools.
 */
export const CATEGORY_GROUPS: { id: CategoryGroup; label: string }[] = [
	{ id: 'personal', label: 'Personal' },
	{ id: 'models', label: 'Models' },
	{ id: 'agents', label: 'Agents and tools' },
	{ id: 'connections', label: 'Connections and security' },
	{ id: 'system', label: 'System and data' }
];

const ROWS: CategoryRow[] = [
	{ id: 'general', label: 'General', icon: SlidersHorizontal, group: 'personal' },
	{ id: 'voice', label: 'Voice', icon: AudioLines, group: 'personal' },
	{ id: 'providers', label: 'Providers', icon: KeyRound, group: 'models' },
	{ id: 'models', label: 'Default models', icon: Sparkles, group: 'models' },
	{ id: 'tuning', label: 'Model tuning', icon: SlidersVertical, group: 'models' },
	{ id: 'usage', label: 'Usage', icon: Gauge, group: 'models' },
	// Not "Agents": that name and the Bot icon belong to the sidebar's Agents
	// area (profiles). This page is the instructions every agent shares.
	{ id: 'agents', label: 'Agent instructions', icon: BotMessageSquare, group: 'agents' },
	{ id: 'memory', label: 'Memory', icon: Brain, group: 'agents' },
	{ id: 'learning', label: 'Learned behaviour', icon: GraduationCap, group: 'agents' },
	{ id: 'plugins', label: 'Plugins', icon: Puzzle, group: 'agents' },
	{ id: 'mcp', label: 'MCP servers', icon: Cable, group: 'agents' },
	{ id: 'hooks', label: 'Hooks', icon: Webhook, group: 'agents' },
	{ id: 'devices', label: 'Devices', icon: MonitorSmartphone, group: 'connections' },
	{ id: 'cloud', label: 'Cloud', icon: Cloud, group: 'connections' },
	// GitHub sign-in and API users (outside apps calling this worker with a key).
	{ id: 'integrations', label: 'Integrations', icon: Plug, group: 'connections' },
	{ id: 'security', label: 'Security', icon: Shield, group: 'connections' },
	{ id: 'vault', label: 'Vault', icon: Vault, group: 'connections' },
	{ id: 'system', label: 'System', icon: Server, group: 'system' },
	{ id: 'logs', label: 'Logs', icon: ScrollText, group: 'system' }
];

export const CATEGORIES: CategoryDef[] = ROWS.map((c) => ({ ...c, type: PAGE_TYPES[c.id] }));

const BY_ID = new Map(CATEGORIES.map((c) => [c.id, c]));

export function categoryById(id: CategoryId): CategoryDef {
	return BY_ID.get(id)!;
}

export function categoryGroupLabel(group: CategoryGroup): string {
	return CATEGORY_GROUPS.find((g) => g.id === group)?.label ?? '';
}
