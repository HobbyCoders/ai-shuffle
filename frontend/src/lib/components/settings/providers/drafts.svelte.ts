/**
 * Credentials being set up. Every "+" choice, "Set up" and "Add provider"
 * makes exactly one draft row in the list; its setup runs in the detail.
 * Drafts are local until their setup saves something: a key setup creates its
 * row on Continue, a device sign-in creates a placeholder row it owns. Those
 * rows stay hidden behind the draft until the setup finishes.
 */
import { deleteModelProvider } from '$lib/api/model-providers';
import { draftName, type CredentialFlavor, type DraftCredential, type ProviderId } from './providers-model';

/** start: the first form; run: a sign-in in progress; models: pick the models to offer. */
export type DraftStep = 'start' | 'run' | 'models';

export interface Draft extends DraftCredential {
	step: DraftStep;
	/** Draft device rows are deleted when the draft is dropped; key rows are kept. */
	ownsRow: boolean;
}

class ProviderDrafts {
	list = $state<Draft[]>([]);
	/** New custom gateways (provider id and name typed so far). */
	gateways = $state<{ id: `gw:${string}`; name: string }[]>([]);
	#n = 0;

	get(item: string | null | undefined): Draft | undefined {
		return item ? this.list.find((d) => d.item === item) : undefined;
	}

	/** One draft credential for a provider. `existing` names its current credentials. */
	add(providerId: ProviderId, flavor: CredentialFlavor, existing: { name: string }[]): Draft {
		const n = ++this.#n;
		const draft: Draft = {
			item: `draft:${n}`,
			providerId,
			flavor,
			name: draftName(providerId, flavor, [...existing, ...this.list.filter((d) => d.providerId === providerId)]),
			step: 'start',
			rowId: null,
			ownsRow: false
		};
		this.list = [...this.list, draft];
		return draft;
	}

	/** "Add provider": a new gateway provider with its one draft credential. */
	addGateway(): Draft {
		const id = `gw:draft-${++this.#n}` as const;
		this.gateways = [...this.gateways, { id, name: 'New provider' }];
		return this.add(id, 'gateway', []);
	}

	update(item: string, patch: Partial<Draft>): void {
		this.list = this.list.map((d) => (d.item === item ? { ...d, ...patch } : d));
	}

	renameGateway(id: ProviderId, name: string): void {
		this.gateways = this.gateways.map((g) => (g.id === id ? { ...g, name: name.trim() || 'New provider' } : g));
	}

	/** Drop a finished or cancelled draft (and its new gateway provider). */
	remove(item: string): void {
		const draft = this.get(item);
		this.list = this.list.filter((d) => d.item !== item);
		if (draft && !this.list.some((d) => d.providerId === draft.providerId)) {
			this.gateways = this.gateways.filter((g) => g.id !== draft.providerId);
		}
	}

	/**
	 * Settings closed: forget every draft. A device sign-in's placeholder row is
	 * deleted (best-effort) so no unsigned row is left behind.
	 */
	discardAll(): void {
		for (const d of this.list) {
			if (d.ownsRow && d.rowId) void deleteModelProvider(d.rowId).catch(() => {});
		}
		this.list = [];
		this.gateways = [];
	}
}

export const providerDrafts = new ProviderDrafts();
