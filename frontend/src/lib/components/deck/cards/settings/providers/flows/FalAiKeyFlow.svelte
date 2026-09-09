<script lang="ts">
	import { Loader2 } from 'lucide-svelte';
	import { apiBase, withAuth } from '$lib/api/runtime';
	import { providers } from '$lib/stores/providers.svelte';
	import AIToolsSettings from '../../AIToolsSettings.svelte';

	interface Props {
		onDone: () => void;
		onCancel: () => void;
		onBusyChange?: (busy: boolean) => void;
	}
	let { onDone, onCancel, onBusyChange }: Props = $props();
	let key = $state('');
	let saving = $state(false);
	let saved = $state(false);
	let error = $state('');
	let verified = $state(false);
	$effect(() => { onBusyChange?.(saving); });

	async function save() {
		if (!key.trim() || saving) return;
		saving = true;
		error = '';
		try {
			const res = await fetch(`${apiBase()}/settings/integrations/fal-ai`, withAuth({
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ api_key: key.trim() })
			}));
			const data = await res.json();
			if (!res.ok) throw new Error(data.detail ?? `Save failed (${res.status})`);
			verified = !!data.valid;
			key = '';
			await providers.loadIntegrations();
			saved = true;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not save fal.ai key';
		} finally { saving = false; }
	}
</script>

<div class="space-y-4">
	{#if saved}
		<p role="status" class="text-sm text-muted-foreground">{verified ? 'API key saved and verified.' : 'API key saved, but verification was unavailable.'} Choose your media models below. Changes save automatically.</p>
		<AIToolsSettings />
		<div class="flex justify-end">
			<button type="button" onclick={onDone} class="btn btn-primary px-4 py-2">Done</button>
		</div>
	{:else}
		<p class="text-sm text-muted-foreground">Add your fal.ai API key for image, video, audio and 3D generation, then choose your media models.</p>
		{#if error}<p role="alert" class="text-sm text-destructive">{error}</p>{/if}
		<div>
			<label for="fal-key" class="block text-sm font-medium text-foreground mb-1.5">API key</label>
			<input id="fal-key" type="password" bind:value={key} autocomplete="off" spellcheck="false" disabled={saving} class="input w-full" />
		</div>
		<div class="flex justify-end gap-3">
			<button type="button" onclick={onCancel} disabled={saving} class="btn btn-secondary px-4 py-2">Back</button>
			<button type="button" onclick={save} disabled={saving || !key.trim()} class="btn btn-primary px-4 py-2 flex items-center gap-2">
				{#if saving}<Loader2 size={16} class="animate-spin" />Saving…{:else}Continue{/if}
			</button>
		</div>
	{/if}
</div>
