<script lang="ts">
	import { Loader2 } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { createModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import ProviderModelsFlow from './ProviderModelsFlow.svelte';

	interface Props {
		kind: 'openai' | 'xai' | 'openrouter';
		onDone: () => void;
		onCancel: () => void;
		onBusyChange?: (busy: boolean) => void;
	}
	let { kind, onDone, onCancel, onBusyChange }: Props = $props();
	const configs = {
		openai: { name: 'OpenAI', base_url: 'https://api.openai.com/v1', placeholder: 'sk-…' },
		xai: { name: 'Grok', base_url: 'https://api.x.ai/v1', placeholder: 'xai-…' },
		openrouter: { name: 'OpenRouter', base_url: 'https://openrouter.ai/api', placeholder: 'sk-or-…' }
	};
	const config = $derived(configs[kind]);
	let key = $state('');
	let createdId = $state<string | null>(null);
	let picking = $state(false);
	let creating = $state(false);
	let error = $state('');
	$effect(() => { if (!picking) onBusyChange?.(creating); });

	async function createAndContinue() {
		if (!key.trim() || creating) return;
		creating = true;
		error = '';
		try {
			// Keep the id on refresh failure so retry never creates a duplicate.
			if (!createdId) {
				const row = await createModelProvider({ name: config.name, provider_type: kind, base_url: config.base_url, auth_token: key.trim() });
				createdId = row.id;
			}
			await providers.refreshProviders();
			picking = true;
		} catch (e) {
			error = getErrorMessage(e, 'Could not add ' + config.name + ' provider');
		} finally { creating = false; }
	}
</script>

{#if picking && createdId}
	<ProviderModelsFlow providerId={createdId} {onDone} {onBusyChange} />
{:else}
	<div class="space-y-4">
		<p class="text-sm text-muted-foreground leading-relaxed">Paste your {config.name} API key. Next, choose which models to offer.</p>
		{#if error}
			<div class="p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-sm text-destructive" role="alert">
				{error}
			</div>
		{/if}

		<div>
			<label for="{kind}-key" class="block text-sm font-medium text-foreground mb-1.5">API key</label>
			<input
				id="{kind}-key"
				type="password"
				autocomplete="off"
				spellcheck="false"
				bind:value={key}
				placeholder={config.placeholder}
				disabled={creating}
				class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:ring-2 focus:ring-ring disabled:opacity-50"
			/>
		</div>

		<div class="flex items-center justify-end gap-3 pt-1">
			<button
				type="button"
				onclick={onCancel}
				disabled={creating}
				class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
			>
				Back
			</button>
			<button
				type="button"
				onclick={createAndContinue}
				disabled={creating || !key.trim()}
				class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center gap-2"
			>
				{#if creating}<Loader2 class="w-4 h-4 animate-spin" />Creating…{:else}Continue{/if}
			</button>
		</div>
	</div>
{/if}
