/**
 * Team templates: optional starter agents the owner installs from the Agents
 * page. AI Shuffle ships no built-in subagents; installing a template creates
 * ordinary agents (fresh ids, editable and deletable like any other).
 *
 *   GET  /api/v1/agents/team-templates                 -> TeamTemplate[]
 *   POST /api/v1/agents/templates/{id}/install         -> TeamTemplate
 *
 * Both are owner-only. A worker without the list endpoint answers 404; callers
 * then get an empty list and offer nothing. Installing creates only the roles
 * that are missing, so a second install is harmless.
 */
import { api } from './client';

export const STARTER_TEAM_ID = 'starter-team';
export const TEAM_TEMPLATES_PATH = '/agents/team-templates';

export interface TeamTemplateAgent {
	key: string;
	name: string;
	/** The ordinary agent filling this role, or null when it is missing. */
	existing_agent_id: string | null;
}

export interface TeamTemplate {
	id: string;
	name: string;
	description: string;
	agents: TeamTemplateAgent[];
}

function isNotFound(err: unknown): boolean {
	return typeof err === 'object' && err !== null && (err as { status?: unknown }).status === 404;
}

function normalizeAgent(raw: unknown): TeamTemplateAgent | null {
	const a = (raw ?? {}) as Partial<TeamTemplateAgent>;
	if (typeof a.key !== 'string' || !a.key) return null;
	return {
		key: a.key,
		name: typeof a.name === 'string' && a.name ? a.name : a.key,
		existing_agent_id: typeof a.existing_agent_id === 'string' && a.existing_agent_id ? a.existing_agent_id : null
	};
}

/** Normalise whatever the server sent into the contract shape; null when unusable. */
export function normalizeTeamTemplate(raw: unknown): TeamTemplate | null {
	const t = (raw ?? {}) as Partial<TeamTemplate>;
	if (typeof t.id !== 'string' || !t.id) return null;
	const agents = Array.isArray(t.agents)
		? t.agents.map(normalizeAgent).filter((a): a is TeamTemplateAgent => a !== null)
		: [];
	return {
		id: t.id,
		name: typeof t.name === 'string' && t.name ? t.name : t.id,
		description: typeof t.description === 'string' ? t.description : '',
		agents
	};
}

/** Every team template, or [] when this worker has no such endpoint (404). */
export async function listTeamTemplates(): Promise<TeamTemplate[]> {
	try {
		const raw = await api.get<unknown>(TEAM_TEMPLATES_PATH);
		return Array.isArray(raw)
			? raw.map(normalizeTeamTemplate).filter((t): t is TeamTemplate => t !== null)
			: [];
	} catch (err) {
		if (isNotFound(err)) return [];
		throw err;
	}
}

/** Create the template's missing agents; answers the template's new state. */
export async function installTeamTemplate(id: string): Promise<TeamTemplate> {
	const raw = await api.post<unknown>(`/agents/templates/${encodeURIComponent(id)}/install`);
	const template = normalizeTeamTemplate(raw);
	if (!template) throw new Error('The server sent an unreadable template.');
	return template;
}

/** True once any of the template's roles is filled by an existing agent. */
export function anyRoleInstalled(template: TeamTemplate): boolean {
	return template.agents.some((a) => a.existing_agent_id !== null);
}
