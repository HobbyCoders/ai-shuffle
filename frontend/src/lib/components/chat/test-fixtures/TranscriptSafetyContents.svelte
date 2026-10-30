<script lang="ts">
	import { getContext } from 'svelte';
	import { TRANSCRIPT_PRESENTATION, type TranscriptPresentation } from '../TranscriptHydrationBoundary.svelte';
	import MessageActions from '../MessageActions.svelte';
	import RunningTasksList from '../RunningTasksList.svelte';
	import MediaLightbox from '../MediaLightbox.svelte';
	import GenerationChoiceCard from '../GenerationChoiceCard.svelte';
	import type { GenerationOption } from '$lib/utils/generation-choice';
	const options: GenerationOption[] = [{
		source: 'fal', account: 'test', worker: 'test', model: 'test-image', kind: 'image',
		options: {}, quick_settings: {}, schema: { fields: [] }, available: true, verified: true,
		price: { currency: 'USD', unit: 'item', amount: '0', total: '0', status: 'included' }
	}];
	let { mode, role, onRewindComplete }: {
		mode: 'message' | 'tasks' | 'media' | 'generation' | 'generation-save'; role: 'user' | 'assistant';
		onRewindComplete?: (warning?: string, prompt?: string, uploads?: string[]) => void;
	} = $props();
	const presentation = getContext<TranscriptPresentation>(TRANSCRIPT_PRESENTATION);
</script>
{#if mode === 'generation' || mode === 'generation-save'}
	<GenerationChoiceCard kind="image" toolId="generation-a" options={mode === 'generation-save' ? options : []} />
{:else if mode === 'tasks'}
	<RunningTasksList sessionId={$presentation.sessionId} presentation={$presentation} />
{:else if mode === 'media'}
	<MediaLightbox />
{:else}
	<MessageActions sessionId={$presentation.sessionId} messageContent="Outgoing prompt"
		messageRole={role} messageUuid="checkpoint-a" {onRewindComplete} />
{/if}
