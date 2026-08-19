<script lang="ts">
	/**
	 * GrokKeyFlow — the "Grok API key" Add-provider flow.
	 *
	 * Dead-simple: one key field, one "Add" button, zero model picking. On submit:
	 *   1. create the provider (`provider_type: 'xai'`, base `https://api.x.ai/v1`);
	 *   2. fetch its catalog (`source:'preset'`, the built-in xAI roster);
	 *   3. persist the FULL roster as the provider's models;
	 *   4. re-hydrate the registry;
	 *   5. `onDone()`.
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

	let createdId: string | null = null;

	$effect(() => {
		onBusyChange?.(submitting);
	});

	async function submit() {
		const token = key.trim();
		if (!token || submitting) return;
		submitting = true;
		error = '';
		try {
			if (!createdId) {
				const created = await createModelProvider({
					name: 'Grok',
					provider_type: 'xai',
					base_url: 'https://api.x.ai/v1',
					auth_token: token,
					default_model: 'grok-4.6',
					small_fast_model: 'grok-4.3'
				});
				createdId = created.id;
			}
			const fetched = await providers.fetchProviderModels(createdId);
			await updateModelProvider(createdId, { models: fetched.models ?? [] });
			await providers.refreshProviders();
			onDone();
		} catch (e) {
			error = getErrorMessage(e, 'Could not add Grok provider');
		} finally {
			submitting = false;
		}
	}
</script>

<div class="space-y-4">
	<p class="text-sm text-muted-foreground leading-relaxed">
		Paste your xAI API key. The standard Grok roster is set up for you — no model picking required.
	</p>

	{#if error}
		<div class="p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-sm text-destructive" role="alert">
			{error}
		</div>
	{/if}

	<div>
		<label for="xai-key" class="block text-sm font-medium text-foreground mb-1.5">API key</label>
		<input
			id="xai-key"
			type="password"
			autocomplete="off"
			spellcheck="false"
			bind:value={key}
			placeholder="xai-…"
			disabled={submitting}
			class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
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
