/**
 * An agent's saved approvals: the permanent allow/deny rules a permission
 * prompt saves when the owner picks "Always for this agent". Owner only.
 *
 *   GET    /api/v1/agents/{id}/permission-rules            newest first
 *   DELETE /api/v1/agents/{id}/permission-rules/{rule_id}  204
 *   DELETE /api/v1/agents/{id}/permission-rules            { deleted_count }
 *
 * A null `tool_pattern` means the rule covers every call of the tool.
 */
import { api } from './client';

export interface PermissionRule {
	id: string;
	profile_id: string;
	tool_name: string;
	tool_pattern: string | null;
	decision: 'allow' | 'deny';
	created_at: string;
}

const base = (profileId: string) => `/agents/${encodeURIComponent(profileId)}/permission-rules`;

function normalize(raw: unknown): PermissionRule | null {
	if (!raw || typeof raw !== 'object') return null;
	const r = raw as Record<string, unknown>;
	if (typeof r.id !== 'string' || typeof r.tool_name !== 'string') return null;
	const decision = r.decision === 'deny' ? 'deny' : r.decision === 'allow' ? 'allow' : null;
	if (!decision) return null;
	return {
		id: r.id,
		profile_id: typeof r.profile_id === 'string' ? r.profile_id : '',
		tool_name: r.tool_name,
		tool_pattern: typeof r.tool_pattern === 'string' && r.tool_pattern.trim() ? r.tool_pattern : null,
		decision,
		created_at: typeof r.created_at === 'string' ? r.created_at : ''
	};
}

export async function listPermissionRules(profileId: string): Promise<PermissionRule[]> {
	const raw = await api.get<unknown>(base(profileId));
	return Array.isArray(raw) ? raw.map(normalize).filter((r): r is PermissionRule => r !== null) : [];
}

export async function deletePermissionRule(profileId: string, ruleId: string): Promise<void> {
	await api.delete(`${base(profileId)}/${encodeURIComponent(ruleId)}`);
}

export async function deleteAllPermissionRules(profileId: string): Promise<number> {
	const res = (await api.delete<{ deleted_count?: unknown }>(base(profileId))) ?? {};
	return typeof res.deleted_count === 'number' ? res.deleted_count : 0;
}

/** Plain text for a failed call: the server's detail when it sent one. */
export function ruleErrorText(error: unknown, fallback: string): string {
	const e = error as { status?: unknown; detail?: unknown } | null;
	if (e?.status === 403) return 'Only the owner can see or change saved approvals.';
	if (typeof e?.detail === 'string' && e.detail.trim()) return e.detail;
	return fallback;
}
