<script lang="ts">
	/**
	 * AddProviderModal — the "Add a provider" modal (plan §1.2).
	 *
	 * A `role="dialog"` modal following the `McpServerForm` precedent: self-contained
	 * Tailwind utility classes, so it is immune to the shell-owned `.ed-content`
	 * global CSS (landmine L3). Fullscreen at ≤1080px via `isPanelFullscreenWidth`.
	 *
	 * Internal step machine: `choose` (five friendly cards) → one step per flow.
	 * Each flow gets `{ onDone, onCancel, onBusyChange }`:
	 *   - `onDone`   closes the modal (a provider/credential was added).
	 *   - `onCancel` returns to `choose`.
	 *   - `onBusyChange` reports whether a flow has an in-flight operation; Esc and
	 *     backdrop close are guarded while busy.
	 *
	 * The Codex ("OpenAI subscription") card routes to `ChatGptDeviceFlow`, a stub
	 * this chunk — Chunk 6 wires the real device flow.
	 */
	import { onMount } from 'svelte';
	import { X, ArrowLeft, Sparkles, MessageSquare, KeyRound, Bot, Network } from 'lucide-svelte';
	import { isPanelFullscreenWidth } from '$lib/utils/viewport';
	import OpenAiKeyFlow from './flows/OpenAiKeyFlow.svelte';
	import ChatGptDeviceFlow from './flows/ChatGptDeviceFlow.svelte';
	import ClaudeApiKeyFlow from './flows/ClaudeApiKeyFlow.svelte';
	import ClaudeSubscriptionFlow from './flows/ClaudeSubscriptionFlow.svelte';
	import OpenRouterKeyFlow from './flows/OpenRouterKeyFlow.svelte';

	interface Props {
		/** Close the modal (from a completed flow, Esc, backdrop, or the ✕). */
		onClose: () => void;
	}
	let { onClose }: Props = $props();

	type Step = 'choose' | 'openai-key' | 'openai-sub' | 'claude-key' | 'claude-sub' | 'openrouter-key';
	let step = $state<Step>('choose');
	// True while the active flow has an operation in flight — guards Esc/backdrop.
	let busy = $state(false);

	// Fullscreen at handheld widths (mirrors the drawer convention, viewport.ts).
	let viewportWidth = $state(typeof window !== 'undefined' ? window.innerWidth : 1280);
	let fullscreen = $derived(isPanelFullscreenWidth(viewportWidth));

	// Esc is handled via keydown on the dialog container, which only fires while
	// focus is inside it — so focus it on open.
	let dialogEl: HTMLDivElement;

	onMount(() => {
		dialogEl?.focus();
		const onResize = () => (viewportWidth = window.innerWidth);
		window.addEventListener('resize', onResize);
		return () => window.removeEventListener('resize', onResize);
	});

	const TITLES: Record<Step, string> = {
		choose: 'Add a provider',
		'openai-key': 'OpenAI API key',
		'openai-sub': 'OpenAI subscription',
		'claude-key': 'Anthropic API key',
		'claude-sub': 'Claude Code subscription',
		'openrouter-key': 'OpenRouter API key'
	};

	const CHOICES: { step: Step; icon: typeof KeyRound; name: string; subtitle: string }[] = [
		{ step: 'openai-key', icon: Sparkles, name: 'OpenAI API key', subtitle: 'Paste a key. Models are set up for you.' },
		{ step: 'openai-sub', icon: MessageSquare, name: 'OpenAI subscription', subtitle: 'Sign in with ChatGPT. No key needed.' },
		{ step: 'claude-key', icon: KeyRound, name: 'Anthropic API key', subtitle: 'Paste an Anthropic key for Claude Code.' },
		{ step: 'claude-sub', icon: Bot, name: 'Claude Code subscription', subtitle: 'Sign in with your Claude account.' },
		{ step: 'openrouter-key', icon: Network, name: 'OpenRouter API key', subtitle: 'One key, hundreds of models to choose from.' }
	];

	function onDone() {
		onClose();
	}
	function onCancel() {
		step = 'choose';
	}
	function onBusyChange(b: boolean) {
		busy = b;
	}

	function requestClose() {
		if (!busy) onClose();
	}
	function goBack() {
		if (!busy) step = 'choose';
	}
	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') requestClose();
	}
</script>

<div
	bind:this={dialogEl}
	class="fixed inset-0 z-50 flex items-center justify-center {fullscreen ? 'p-0' : 'p-4'}"
	role="dialog"
	aria-modal="true"
	aria-label={TITLES[step]}
	onkeydown={handleKeydown}
	tabindex="-1"
>
	<!-- Backdrop -->
	<button
		class="absolute inset-0 bg-background/80 backdrop-blur-sm"
		onclick={requestClose}
		aria-label="Close"
	></button>

	<!-- Modal card -->
	<div
		class="relative flex flex-col bg-card border border-border shadow-xl {fullscreen
			? 'w-full h-full max-w-none rounded-none'
			: 'w-full max-w-lg max-h-[90vh] rounded-xl'}"
	>
		<!-- Header -->
		<div class="flex items-center gap-2 px-6 py-4 border-b border-border">
			{#if step !== 'choose'}
				<button
					type="button"
					onclick={goBack}
					disabled={busy}
					class="p-1.5 -ml-1.5 hover:bg-muted rounded-lg transition-colors disabled:opacity-50"
					aria-label="Back"
				>
					<ArrowLeft class="w-5 h-5 text-muted-foreground" />
				</button>
			{/if}
			<h2 class="flex-1 text-lg font-semibold text-foreground">{TITLES[step]}</h2>
			<button
				type="button"
				onclick={requestClose}
				disabled={busy}
				class="p-2 -mr-2 hover:bg-muted rounded-lg transition-colors disabled:opacity-50"
				aria-label="Close"
			>
				<X class="w-5 h-5 text-muted-foreground" />
			</button>
		</div>

		<!-- Content -->
		<div class="flex-1 overflow-y-auto p-6">
			{#if step === 'choose'}
				<div class="flex flex-col gap-2.5">
					{#each CHOICES as choice (choice.step)}
						{@const Icon = choice.icon}
						<button
							type="button"
							onclick={() => (step = choice.step)}
							class="flex items-center gap-4 text-left p-4 rounded-xl border border-border bg-background hover:border-foreground/40 hover:bg-muted transition-colors"
						>
							<span class="flex-shrink-0 grid place-items-center w-11 h-11 rounded-lg bg-muted text-foreground">
								<Icon class="w-5 h-5" />
							</span>
							<span class="min-w-0">
								<span class="block text-sm font-semibold text-foreground">{choice.name}</span>
								<span class="block text-xs text-muted-foreground mt-0.5">{choice.subtitle}</span>
							</span>
						</button>
					{/each}
				</div>
			{:else if step === 'openai-key'}
				<OpenAiKeyFlow {onDone} {onCancel} {onBusyChange} />
			{:else if step === 'openai-sub'}
				<ChatGptDeviceFlow {onDone} {onCancel} {onBusyChange} />
			{:else if step === 'claude-key'}
				<ClaudeApiKeyFlow {onDone} {onCancel} {onBusyChange} />
			{:else if step === 'claude-sub'}
				<ClaudeSubscriptionFlow {onDone} {onCancel} {onBusyChange} />
			{:else if step === 'openrouter-key'}
				<OpenRouterKeyFlow {onDone} {onCancel} {onBusyChange} />
			{/if}
		</div>
	</div>
</div>
