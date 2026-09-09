<script lang="ts" module>
	/** One screen in the add-provider flow: the chooser, or a single flow. */
	export type Step =
		| 'choose'
		| 'openai-key'
		| 'openai-sub'
		| 'grok-key'
		| 'grok-sub'
		| 'claude-key'
		| 'claude-sub'
		| 'openrouter-key'
		| 'fal-key';
</script>

<script lang="ts">
	/**
	 * AddProviderModal — shared by setup and Settings.
	 *
	 * A `role="dialog"` modal following the `McpServerForm` precedent: self-contained
	 * Tailwind utility classes, so it is immune to the shell-owned `.ed-content`
	 * global CSS (landmine L3). Fullscreen on the mobile shell; a centred card,
	 * clamped to the viewport, on the desktop shell at every width.
	 *
	 * Internal step machine: chooser → credentials → model selection where supported.
	 * Each flow gets `{ onDone, onCancel, onBusyChange }`:
	 *   - `onDone`   closes the modal (a provider/credential was added).
	 *   - `onCancel` returns to `choose`.
	 *   - `onBusyChange` reports whether a flow has an in-flight operation; Esc and
	 *     backdrop close are guarded while busy.
	 *
	 * New ChatGPT and Grok subscriptions continue from device sign-in to the
	 * same model picker used by API keys. Reconnect flows keep their saved roster.
	 */
	import { onMount } from 'svelte';
	import { X, ArrowLeft, Sparkles, MessageSquare, KeyRound, Bot, Network, Zap } from 'lucide-svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import OpenAiKeyFlow from './flows/OpenAiKeyFlow.svelte';
	import ChatGptDeviceFlow from './flows/ChatGptDeviceFlow.svelte';
	import GrokKeyFlow from './flows/GrokKeyFlow.svelte';
	import GrokDeviceFlow from './flows/GrokDeviceFlow.svelte';
	import ClaudeApiKeyFlow from './flows/ClaudeApiKeyFlow.svelte';
	import ClaudeSubscriptionFlow from './flows/ClaudeSubscriptionFlow.svelte';
	import OpenRouterKeyFlow from './flows/OpenRouterKeyFlow.svelte';
	import FalAiKeyFlow from './flows/FalAiKeyFlow.svelte';
	import ProviderModelsFlow from './flows/ProviderModelsFlow.svelte';

	interface Props {
		/** Close the modal (from a completed flow, Esc, backdrop, or the ✕). */
		onClose: () => void;
		/**
		 * Open straight on one flow instead of the chooser. The provider list
		 * uses this so a row's own "Sign in" / "Add API key" button lands on that
		 * provider's screen. There is no chooser to return to in that journey, so
		 * Back and Cancel close the modal.
		 */
		initialStep?: Step;
	}
	let { onClose, initialStep }: Props = $props();

	// The modal is mounted fresh per open, so the opening step is read once.
	// svelte-ignore state_referenced_locally
	let step = $state<Step>(initialStep ?? 'choose');
	/** True when the modal skipped the chooser — Back/Cancel close instead. */
	// svelte-ignore state_referenced_locally
	const pinnedToStep = initialStep !== undefined && initialStep !== 'choose';
	// True while the active flow has an operation in flight — guards Esc/backdrop.
	let busy = $state(false);
	let modelsProviderId = $state<string | null>(null);

	// Fullscreen on the mobile shell only (mirrors the drawer convention).
	const fullscreen = $derived(platform.isMobile);

	// Esc is handled via keydown on the dialog container, which only fires while
	// focus is inside it — so focus it on open.
	let dialogEl: HTMLDivElement;

	onMount(() => {
		dialogEl?.focus();
	});

	const TITLES: Record<Step, string> = {
		choose: 'Add a provider',
		'openai-key': 'OpenAI API key',
		'openai-sub': 'OpenAI subscription',
		'grok-key': 'Grok API key',
		'grok-sub': 'Grok subscription',
		'claude-key': 'Anthropic API key',
		'claude-sub': 'Claude Code subscription',
		'openrouter-key': 'OpenRouter API key',
		'fal-key': 'fal.ai API key'
	};

	const CHOICES: { step: Step; icon: typeof KeyRound; name: string; subtitle: string }[] = [
		{ step: 'openai-key', icon: Sparkles, name: 'OpenAI API key', subtitle: 'Paste a key and choose your models.' },
		{ step: 'openai-sub', icon: MessageSquare, name: 'OpenAI subscription', subtitle: 'Sign in with ChatGPT. No key needed.' },
		{ step: 'grok-key', icon: Zap, name: 'Grok API key', subtitle: 'Paste a key and choose your models.' },
		{ step: 'grok-sub', icon: Sparkles, name: 'Grok subscription', subtitle: 'Sign in with SuperGrok. No key needed.' },
		{ step: 'claude-key', icon: KeyRound, name: 'Anthropic API key', subtitle: 'Paste an Anthropic key for Claude Code.' },
		{ step: 'claude-sub', icon: Bot, name: 'Claude Code subscription', subtitle: 'Sign in with your Claude account.' },
		{ step: 'openrouter-key', icon: Network, name: 'OpenRouter API key', subtitle: 'One key, hundreds of models to choose from.' },
		{ step: 'fal-key', icon: Sparkles, name: 'fal.ai API key', subtitle: 'Choose models for image, video, audio and 3D tools.' }
	];

	function onDone() {
		onClose();
	}
	function onConnected(providerId: string) {
		modelsProviderId = providerId;
	}
	function onCancel() {
		if (pinnedToStep) onClose();
		else step = 'choose';
	}
	function onBusyChange(b: boolean) {
		busy = b;
	}

	function requestClose() {
		if (!busy) onClose();
	}
	function goBack() {
		if (busy) return;
		if (pinnedToStep || modelsProviderId) onClose();
		else step = 'choose';
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
	aria-label={modelsProviderId ? 'Choose models' : TITLES[step]}
	onkeydown={handleKeydown}
	tabindex="-1"
>
	<!-- Backdrop -->
	<button
		class="absolute inset-0 bg-background/80 backdrop-blur-xs"
		onclick={requestClose}
		aria-label="Close"
	></button>

	<!-- Modal card -->
	<div
		class="relative flex flex-col bg-card border border-border shadow-xl {fullscreen
			? 'w-full h-full max-w-none rounded-none'
			: `w-full ${step === 'fal-key' ? 'max-w-3xl' : 'max-w-lg'} max-h-[90vh] rounded-xl`}"
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
			<h2 class="flex-1 text-lg font-semibold text-foreground">{modelsProviderId ? 'Choose models' : TITLES[step]}</h2>
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
			{#if modelsProviderId}
				<ProviderModelsFlow providerId={modelsProviderId} {onDone} {onBusyChange} />
			{:else if step === 'choose'}
				<div class="flex flex-col gap-2.5">
					{#each CHOICES as choice (choice.step)}
						{@const Icon = choice.icon}
						<button
							type="button"
							onclick={() => (step = choice.step)}
							class="flex items-center gap-4 text-left p-4 rounded-xl border border-border bg-background hover:border-foreground/40 hover:bg-muted transition-colors"
						>
							<span class="shrink-0 grid place-items-center w-11 h-11 rounded-lg bg-muted text-foreground">
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
				<ChatGptDeviceFlow {onDone} {onCancel} {onBusyChange} {onConnected} />
			{:else if step === 'grok-key'}
				<GrokKeyFlow {onDone} {onCancel} {onBusyChange} />
			{:else if step === 'grok-sub'}
				<GrokDeviceFlow {onDone} {onCancel} {onBusyChange} {onConnected} />
			{:else if step === 'claude-key'}
				<ClaudeApiKeyFlow {onDone} {onCancel} {onBusyChange} />
			{:else if step === 'claude-sub'}
				<ClaudeSubscriptionFlow {onDone} {onCancel} {onBusyChange} />
			{:else if step === 'openrouter-key'}
				<OpenRouterKeyFlow {onDone} {onCancel} {onBusyChange} />
			{:else if step === 'fal-key'}
				<FalAiKeyFlow {onDone} {onCancel} {onBusyChange} />
			{/if}
		</div>
	</div>
</div>
