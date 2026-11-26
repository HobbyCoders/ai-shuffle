/**
 * Proposed memories: the inbox behind Settings > Memory > Proposed memories.
 *
 * Session reflection (Memory v3) saves a rule or preference that quotes the
 * owner at once. Anything else it notices waits here: it becomes an
 * agent-saved memory only when the owner accepts it, and otherwise expires;
 * a dismissed one never comes back. "From N chats" is information only.
 * Proposals and decisions sync to every worker; agents never see a proposal
 * until it is saved.
 *
 * GET /memories/proposals, POST /memories/proposals/{id}/accept|dismiss.
 */
import { api } from '$lib/api/client';
import { isForbidden } from '$lib/settings/access.svelte';
import type { MemoryItem } from './memory-model.svelte';

export type ProposalKind = 'owner_preference' | 'owner_fact' | 'project';

export interface ProposalSession {
	id: string;
	/** Null when the chat was deleted or ran on another worker. */
	title: string | null;
}

export interface Proposal {
	id: string;
	text: string;
	kind: ProposalKind;
	kind_label: string;
	quote: string | null;
	when: string | null;
	scope: string;
	scope_label: string;
	sessions: ProposalSession[];
	session_count: number;
	created_at: number | null;
	expires_at: number | null;
}

interface ProposalList {
	items: Proposal[];
	ttl_days: number;
	mode: 'reflection' | 'continuous';
}

interface Decision {
	id: string;
	status: 'accepted' | 'dismissed';
	memory: MemoryItem | null;
}

export const PROPOSALS_ITEM = 'proposals';

const DAY_MS = 86_400_000;

/** "Expires in 12 days", "Expires today". */
export function expiryText(expiresAt: number | null, now = Date.now()): string {
	if (!expiresAt) return '';
	const days = Math.ceil((expiresAt * 1000 - now) / DAY_MS);
	if (days <= 0) return 'Expires today';
	return days === 1 ? 'Expires tomorrow' : `Expires in ${days} days`;
}

/** "From 2 chats", "From 1 chat". */
export function sessionText(p: Pick<Proposal, 'session_count'>): string {
	return `From ${p.session_count.toLocaleString()} ${p.session_count === 1 ? 'chat' : 'chats'}`;
}

export function chatTitle(s: ProposalSession): string {
	return s.title?.trim() || 'A chat on another worker or since deleted';
}

/** The fixed row's summary: "2 waiting for you" or "None waiting". */
export function inboxSummary(count: number): string {
	return count ? `${count.toLocaleString()} waiting for you` : 'None waiting';
}

const statusOf = (e: unknown): number | undefined =>
	e && typeof e === 'object' ? ((e as { status?: number }).status ?? undefined) : undefined;

function detail(e: unknown): string {
	const d = e && typeof e === 'object' ? (e as { detail?: unknown }).detail : undefined;
	return typeof d === 'string' ? d : '';
}

export class ProposalModel {
	items = $state.raw<Proposal[]>([]);
	ttlDays = $state(14);
	mode = $state<'reflection' | 'continuous'>('reflection');
	loading = $state(false);
	loaded = $state(false);
	forbidden = $state(false);
	loadError = $state('');
	/** The proposal a decision is being sent for. */
	busy = $state<string | null>(null);
	error = $state('');
	/** A one-line answer after a decision ("Saved as a memory."). */
	notice = $state('');
	#requests = 0;

	async load(): Promise<boolean> {
		const request = ++this.#requests;
		this.loading = true;
		try {
			const data = await api.get<ProposalList>('/memories/proposals');
			if (request !== this.#requests) return false;
			this.items = data.items ?? [];
			if (data.ttl_days) this.ttlDays = data.ttl_days;
			if (data.mode) this.mode = data.mode;
			this.forbidden = false;
			this.loadError = '';
			this.loaded = true;
			return true;
		} catch (e) {
			if (request === this.#requests) {
				if (isForbidden(e)) this.forbidden = true;
				else this.loadError = 'Could not load proposed memories.';
			}
			return false;
		} finally {
			if (request === this.#requests) this.loading = false;
		}
	}

	/** The saved memory on Accept (null on Dismiss), or undefined when it failed. */
	async decide(id: string, accept: boolean): Promise<MemoryItem | null | undefined> {
		if (this.busy) return undefined;
		this.busy = id;
		this.error = '';
		this.notice = '';
		try {
			const out = await api.post<Decision>(
				`/memories/proposals/${encodeURIComponent(id)}/${accept ? 'accept' : 'dismiss'}`
			);
			this.#requests += 1;
			this.items = this.items.filter((p) => p.id !== id);
			this.notice = accept ? 'Saved as a memory. Agents can use it from the next chat.' : 'Dismissed. Nothing was saved.';
			return out.memory ?? null;
		} catch (e) {
			const status = statusOf(e);
			if (status === 404 || status === 409) {
				// Decided on another worker, or expired: drop it and say so.
				this.items = this.items.filter((p) => p.id !== id);
				this.error = 'This proposal was already decided or has expired.';
			} else if (isForbidden(e)) {
				this.error = 'You need owner access to decide proposed memories.';
			} else if (status === undefined) {
				this.error = 'Could not reach this worker, so this may not have been saved. Try again.';
			} else {
				this.error = detail(e) || 'Could not save this decision.';
			}
			return undefined;
		} finally {
			this.busy = null;
		}
	}
}
