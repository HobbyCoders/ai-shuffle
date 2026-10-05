<script lang="ts">
	import { pwa, isInstallable } from '$lib/stores/pwa.svelte';
	import { installPromptPreference } from '$lib/stores/install-prompt-preference.svelte';
	import NotificationSlot from './NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';

	let showPrompt = $state(false);
	const isDismissed = $derived(installPromptPreference.isSuppressed());

	// Show the prompt with a delay to not be intrusive
	$effect(() => {
		if ($isInstallable && !isDismissed) {
			const timer = setTimeout(() => {
				showPrompt = true;
			}, 3000);
			return () => clearTimeout(timer);
		} else {
			showPrompt = false;
		}
	});

	async function handleInstall() {
		const installed = await pwa.promptInstall();
		if (installed) {
			showPrompt = false;
		}
	}

	function handleDismiss() {
		showPrompt = false;
		installPromptPreference.snooze();
	}

	function handleNeverShow() {
		showPrompt = false;
		installPromptPreference.neverShow();
	}
</script>

{#if showPrompt}
	<!-- Draws in the shared notification dock (#1823), like every other notice. -->
	<NotificationSlot order={DOCK_ORDER.install}>
		<div class="install-card" role="dialog" aria-label="Install AI Shuffle">
			<div class="install-copy">
				<strong>Install AI Shuffle</strong>
				<span>Add it to your home screen for quick access.</span>
			</div>
			<div class="install-actions">
				<button type="button" class="primary" onclick={handleInstall}>Install</button>
				<button type="button" onclick={handleDismiss}>Not now</button>
				<button type="button" class="quiet" onclick={handleNeverShow}>Don't show again</button>
			</div>
		</div>
	</NotificationSlot>
{/if}

<style>
	.install-card {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 10px 16px;
		width: 100%;
		padding: 12px 16px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.66);
		background: var(--card);
		color: var(--card-foreground, var(--foreground));
		box-shadow: var(--shadow-l);
		font-size: 0.875rem;
		line-height: 1.4;
	}
	.install-copy { display: flex; flex-direction: column; gap: 2px; flex: 1 1 200px; min-width: 0; }
	.install-copy span { color: var(--muted-foreground); font-size: 0.8125rem; }
	.install-actions { display: flex; flex-wrap: wrap; gap: 8px; }
	button {
		min-height: 32px;
		padding: 4px 12px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--secondary);
		color: var(--secondary-foreground, var(--foreground));
		font-size: 0.75rem;
		font-weight: 600;
		cursor: pointer;
		transition: opacity 0.15s ease;
	}
	button:hover { opacity: 0.88; }
	button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.primary { background: var(--primary); color: var(--primary-foreground); border-color: transparent; }
	.quiet { background: transparent; border-color: transparent; color: var(--muted-foreground); }
	@media (max-width: 639px), (pointer: coarse) { button { min-height: 44px; } }
	@media (prefers-reduced-motion: reduce) { button { transition: none; } }
</style>
