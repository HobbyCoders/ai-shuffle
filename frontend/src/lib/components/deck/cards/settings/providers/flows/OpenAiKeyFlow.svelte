<script lang="ts">
	/**
	 * OpenAiKeyFlow — the "OpenAI API key" Add-provider flow (plan §1.2 `openai-key`).
	 *
	 * Dead-simple: one key field, one "Add" button, zero model picking. On submit:
	 *   1. create the provider  (`createModelProvider`, so we get its `id` back —
	 *      the store's `saveProvider` returns void, and we need the id for step 2);
	 *   2. fetch its catalog     (`fetchProviderModels` → `source:'preset'`, the
	 *      built-in OpenAI roster from `OPENAI_PRESET_MODELS`);
	 *   3. persist the FULL roster as the provider's models
	 *      (`updateModelProvider(id, { models })`);
	 *   4. re-hydrate the registry (`providers.refreshProviders()` — landmine L4);
	 *   5. `onDone()`.
	 *
	 * Rendered inside `AddProviderModal`, which lives OUTSIDE `.ed-content`, so this
	 * uses self-contained Tailwind utility classes (landmine L3) — never the shell
	 * `.inp`/`.ed-btn` globals.
	 */
	import { Loader2 } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { createModelProvider, updateModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';

	interface Props {
		onDone: () => void;
		onCancel: () => void;
		onBusyChange?: (busy: boolean) => void;
	}
	let { onDone, onCancel, onBusyChange }: Props = $props();

	let key = $state('');
	let submitting = $state(false);
	let error = $state('');

	// Survives a failed fetch/update so a retry reuses the created row instead of
	// creating a duplicate OpenAI provider.
	let createdId: string | null = null;

	// Report busy up so the modal guards Esc / backdrop while a create is in flight.
	$effect(() => {
		onBusyChange?.(submitting);
	});

	async function submit() {
		const token = key.trim();
		if (!token || submitting) return;
		submitting = true;
		error = '';
		try {
			// Create first so we have the persisted id needed to fetch its catalog.
			if (!createdId) {
				const created = await createModelProvider({
					name: 'OpenAI',
					provider_type: 'openai',
					base_url: 'https://api.openai.com/v1',
					auth_token: token
				});
				createdId = created.id;
			}
			// Preset roster — the backend returns `source:'preset'` with the OpenAI
			// lineup; persist the FULL roster, no user picking.
			const fetched = await providers.fetchProviderModels(createdId);
			await updateModelProvider(createdId, { models: fetched.models ?? [] });
			// Re-hydrate the model registry so the profile editor sees the new models.
			await providers.refreshProviders();
			onDone();
		} catch (e) {
			error = getErrorMessage(e, 'Could not add OpenAI provider');
		} finally {
			submitting = false;
		}
	}
</script>

<div class="space-y-4">
	<p class="text-sm text-muted-foreground leading-relaxed">
		Paste your OpenAI API key. The standard model roster is set up for you — no model picking
		required.
	</p>

	{#if error}
		<div class="p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-sm text-destructive" role="alert">
			{error}
		</div>
	{/if}

	<div>
		<label for="openai-key" class="block text-sm font-medium text-foreground mb-1.5">API key</label>
		<input
			id="openai-key"
			type="password"
			autocomplete="off"
			spellcheck="false"
			bind:value={key}
			placeholder="sk-…"
			disabled={submitting}
			class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:ring-2 focus:ring-ring disabled:opacity-50"
		/>
	</div>

	<div class="flex items-center justify-end gap-3 pt-1">
		<button
			type="button"
			onclick={onCancel}
			disabled={submitting}
			class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
		>
			Back
		</button>
		<button
			type="button"
			onclick={submit}
			disabled={submitting || !key.trim()}
			class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center gap-2"
		>
			{#if submitting}<Loader2 class="w-4 h-4 animate-spin" />Adding…{:else}Add{/if}
		</button>
	</div>
</div>
