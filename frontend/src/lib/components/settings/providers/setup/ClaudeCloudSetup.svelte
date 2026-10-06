<script lang="ts">
	/**
	 * Setting up Claude through a cloud provider or gateway: the warning, the
	 * method and its values. Saving finishes the setup; it turns the method on
	 * only when the owner picks "Save and use for Claude".
	 */
	import { onMount } from 'svelte';
	import { getClaudeCloud, type ClaudeCloudConfig } from '$lib/api/claude-auth';
	import { getErrorMessage } from '$lib/api/client';
	import { providers } from '$lib/stores/providers.svelte';
	import ClaudeCloudForm from '../ClaudeCloudForm.svelte';
	import { CLAUDE_CLOUD_ITEM } from '../providers-model';

	interface Props {
		onFinish: (item: string) => void;
		onCancel: () => void;
	}

	let { onFinish, onCancel }: Props = $props();

	let config = $state<ClaudeCloudConfig | null>(null);
	let error = $state('');
	const mode = $derived(providers.current.claude?.mode ?? 'subscription');

	onMount(async () => {
		try {
			config = await getClaudeCloud();
		} catch (e) {
			error =
				(e as { status?: unknown } | null)?.status === 403
					? 'Only the owner of this worker can set this up.'
					: getErrorMessage(e, 'Could not read the cloud settings');
		}
	});
</script>

{#if config}
	<ClaudeCloudForm {config} {mode} onSaved={() => onFinish(CLAUDE_CLOUD_ITEM)} {onCancel} />
{:else if error}
	<p class="pv-msg err" role="alert">{error}</p>
	<div class="pv-acts"><button type="button" class="st-btn ghost" onclick={onCancel}>Cancel</button></div>
{:else}
	<p class="pv-msg">Loading</p>
{/if}
