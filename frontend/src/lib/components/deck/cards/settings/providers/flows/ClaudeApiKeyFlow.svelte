<script lang="ts">
	/**
	 * ClaudeApiKeyFlow — the "Claude Code API key" Add-provider flow
	 * (plan §1.2 `claude-key`).
	 *
	 * Sets the Anthropic API key that Claude Code authenticates with. This is NOT a
	 * `model_providers` row — it hits the existing integrations endpoint exactly as
	 * `ClaudeApiKeyRow.svelte` does (landmine L6 — a relocated raw `fetch`, NOT a
	 * rewrite of auth semantics). Success closes the modal.
	 *
	 * Rendered inside `AddProviderModal` (outside `.ed-content`) → self-contained
	 * Tailwind utility classes only (landmine L3).
	 */
	import { Loader2 } from 'lucide-svelte';
	import { apiBase, withAuth } from '$lib/api/runtime';

	interface Props {
		onDone: () => void;
		onCancel: () => void;
		onBusyChange?: (busy: boolean) => void;
	}
	let { onDone, onCancel, onBusyChange }: Props = $props();

	let key = $state('');
	let submitting = $state(false);
	let error = $state('');

	$effect(() => {
		onBusyChange?.(submitting);
	});

	// Mirror of ClaudeApiKeyRow's save: POST /settings/integrations/anthropic with
	// { api_key }, credentials included, JSON content-type. Same shape, same errors.
	async function submit() {
		const apiKey = key.trim();
		if (!apiKey || submitting) return;
		submitting = true;
		error = '';
		try {
			const res = await fetch(`${apiBase()}/settings/integrations/anthropic`, withAuth({
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ api_key: apiKey })
			}));
			if (!res.ok) {
				const body = await res.json().catch(() => ({ }));
				error = body.detail ?? `Save failed (${res.status})`;
				return;
			}
			onDone();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Network error';
		} finally {
			submitting = false;
		}
	}
</script>

<div class="space-y-4">
	<p class="text-sm text-muted-foreground leading-relaxed">
		Paste an Anthropic API key. Claude Code authenticates with it and it overrides the connected
		account until you remove it.
	</p>

	{#if error}
		<div class="p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-sm text-destructive" role="alert">
			{error}
		</div>
	{/if}

	<div>
		<label for="claude-key" class="block text-sm font-medium text-foreground mb-1.5">Anthropic API key</label>
		<input
			id="claude-key"
			type="password"
			autocomplete="off"
			spellcheck="false"
			bind:value={key}
			placeholder="sk-ant-…"
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
			{#if submitting}<Loader2 class="w-4 h-4 animate-spin" />Saving…{:else}Use this key{/if}
		</button>
	</div>
</div>
