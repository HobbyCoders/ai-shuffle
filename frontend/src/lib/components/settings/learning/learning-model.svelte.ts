/**
 * Settings > Learned behaviour: the memory learner's rules, setup and recent
 * passes (GET /learner), rule writes (/learner/rules), and the on/off switch
 * and daily call budget in the owner policy (/settings/owner-policy).
 */
import { api, getErrorMessage, isApiError } from '$lib/api/client';
import { relativeAgo, shortDate } from '../integrations/api-users.svelte';

export type AppliesTo = 'all' | 'claude' | 'gpt' | 'gemini' | 'grok' | 'other';

export interface LearnedRule {
	id: string;
	revision: number;
	metadata_version: string;
	text: string;
	applies_to: AppliesTo;
	applies_label: string;
	kept: boolean;
	new: boolean;
	previous_text: string | null;
	scope_label: string;
	needs_review: boolean;
	created_at: number | null;
	updated_at: number | null;
	expires_at: number | null;
}

export interface LearnerSetup {
	enabled: boolean;
	daily_calls_setting: number;
	daily_cap: number;
	cap_source: 'setting' | 'subscription_default' | 'metered_default';
	calls_today: number;
	/** The model the latest pass ran on (passes use each chat's own model). */
	model: string | null;
	/** How the latest pass was billed. */
	billing: 'subscription' | 'metered';
	/** Most prompt text one pass may send. */
	pass_chars: number;
}

export interface LearnerPass {
	at: number | null;
	kind: string;
	ran: boolean;
	read: number;
	rules: { id: string; text: string | null }[];
	memories: number;
	input_tokens: number | null;
	output_tokens: number | null;
	outcome: 'ok' | 'error' | 'budget' | 'skipped';
	error_kind: string | null;
}

export interface GuidanceBlock {
	title: string;
	text: string;
	families: string[];
}

export interface LearnerOverview {
	setup: LearnerSetup;
	rules: LearnedRule[];
	activity: LearnerPass[];
	guidance: GuidanceBlock[];
	applies_to: { value: AppliesTo; label: string }[];
	limits: { rule_chars: number; learned_rules: number };
}

export const SETUP = 'setup';
export const ACTIVITY = 'activity';
export const GUIDANCE = 'guidance';
export const NEW_RULE = 'new-rule';
export const FIXED_ITEMS = [SETUP, ACTIVITY, GUIDANCE, NEW_RULE];

const iso = (seconds: number) => new Date(seconds * 1000).toISOString();
export const agoText = (seconds: number | null): string => (seconds ? relativeAgo(iso(seconds)) : '');
export const dateText = (seconds: number | null): string => (seconds ? shortDate(iso(seconds)) : '');
export const compact = (value: string): string => value.split(/\s+/).filter(Boolean).join(' ');

export function setupSummary(s: LearnerSetup): string {
	if (!s.enabled) return 'Off: no learning calls';
	return `On · ${s.calls_today.toLocaleString()} of ${s.daily_cap.toLocaleString()} calls today`;
}

export function capText(s: LearnerSetup): string {
	if (s.cap_source === 'setting') return `${s.daily_cap.toLocaleString()} calls a day (your limit)`;
	return s.cap_source === 'subscription_default'
		? `${s.daily_cap.toLocaleString()} calls a day (automatic, flat-rate subscription)`
		: `${s.daily_cap.toLocaleString()} calls a day (automatic, pay-per-use provider)`;
}

export function ruleMeta(r: LearnedRule): string {
	const who = r.kept ? 'Kept by you' : 'Learned';
	const ago = agoText(r.updated_at);
	return [r.applies_label, who, ago].filter(Boolean).join(' · ');
}

export function passSummary(p: LearnerPass): string {
	if (p.outcome === 'budget') return 'Stopped: daily call limit reached';
	if (p.outcome === 'skipped') return 'Skipped, no model call';
	if (p.outcome === 'error') return `Failed${p.error_kind ? ` (${p.error_kind})` : ''}`;
	const parts = [`Read ${p.read.toLocaleString()} ${p.read === 1 ? 'entry' : 'entries'}`];
	if (p.rules.length) parts.push(`${p.rules.length} ${p.rules.length === 1 ? 'rule' : 'rules'}`);
	if (p.memories) parts.push(`${p.memories} ${p.memories === 1 ? 'memory' : 'memories'}`);
	if (!p.rules.length && !p.memories) parts.push('nothing new');
	return parts.join(' · ');
}

export function tokenText(p: LearnerPass): string {
	if (!p.input_tokens && !p.output_tokens) return '';
	return `${(p.input_tokens ?? 0).toLocaleString()} in / ${(p.output_tokens ?? 0).toLocaleString()} out tokens`;
}

export function validateRule(text: string, limit: number): string | null {
	const body = compact(text);
	if (!body) return 'Write the rule.';
	if (body.length > limit) return `Keep it under ${limit.toLocaleString()} characters.`;
	return null;
}

const FORBIDDEN = 'Sign in as the owner to manage learned behaviour.';

function failure(e: unknown, fallback: string): string {
	return isApiError(e) && e.status === 403 ? FORBIDDEN : getErrorMessage(e, fallback);
}

export class LearningModel {
	data = $state<LearnerOverview | null>(null);
	loading = $state(false);
	loadError = $state('');
	busy = $state('');
	error = $state('');

	rule(id: string | null | undefined): LearnedRule | undefined {
		return id ? this.data?.rules.find((r) => r.id === id) : undefined;
	}

	get newCount(): number {
		return this.data?.rules.filter((r) => r.new).length ?? 0;
	}

	async load(): Promise<void> {
		this.loading = true;
		try {
			this.data = await api.get<LearnerOverview>('/learner');
			this.loadError = '';
		} catch (e) {
			this.loadError = failure(e, 'Could not load learned behaviour.');
		} finally {
			this.loading = false;
		}
	}

	private async run<T>(key: string, fallback: string, write: () => Promise<T>): Promise<T | undefined> {
		this.busy = key;
		this.error = '';
		try {
			const out = await write();
			await this.load();
			return out;
		} catch (e) {
			this.error = failure(e, fallback);
			if (isApiError(e) && (e.status === 409 || e.status === 404)) await this.load();
			return undefined;
		} finally {
			this.busy = '';
		}
	}

	private version(r: LearnedRule) {
		return { expected_revision: r.revision, expected_metadata_version: r.metadata_version };
	}

	create(text: string, applies_to: AppliesTo) {
		return this.run('create', 'Could not add this rule.', () =>
			api.post<LearnedRule>('/learner/rules', { text: compact(text), applies_to })
		);
	}

	save(r: LearnedRule, text: string, applies_to: AppliesTo) {
		return this.run(r.id, 'Could not save this rule.', () =>
			api.put<LearnedRule>(`/learner/rules/${r.id}`, { text: compact(text), applies_to, ...this.version(r) })
		);
	}

	undo(r: LearnedRule) {
		return this.run(r.id, 'Could not undo this change.', () =>
			api.post<{ restored?: LearnedRule; deleted?: string }>(`/learner/rules/${r.id}/undo`, this.version(r))
		);
	}

	async remove(r: LearnedRule): Promise<boolean> {
		const v = this.version(r);
		const done = await this.run(r.id, 'Could not delete this rule.', async () => {
			await api.delete(
				`/learner/rules/${r.id}?expected_revision=${v.expected_revision}&expected_metadata_version=${encodeURIComponent(v.expected_metadata_version)}`
			);
			return true;
		});
		return done === true;
	}

	/** Owner-policy write (shared with every worker); false when it failed. */
	async setPolicy(changes: { learner_enabled?: boolean; learner_daily_calls?: number }): Promise<boolean> {
		const done = await this.run('policy', 'Could not save this setting.', async () => {
			await api.put('/settings/owner-policy', changes);
			return true;
		});
		return done === true;
	}
}
