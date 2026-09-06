<script lang="ts">
	import { MessageSquare, Brain, Monitor } from 'lucide-svelte';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { computerUse } from '$lib/stores/computerUse.svelte';

	interface Props {
		chatPrefs: ChatPreferences | null;
	}

	let { chatPrefs }: Props = $props();

	// Host capability for the computer-use toggle. Native-only: unavailable in
	// Docker/headless, which we surface so the toggle can be greyed out. The
	// shared poller only runs while this section is mounted.
	let toggleBusy = $state(false);
	$effect(() => {
		computerUse.start();
		return () => computerUse.stop();
	});

	const UNAVAILABLE_REASONS: Record<string, string> = {
		docker: 'AI Shuffle is running in Docker, which cannot control the host desktop. Run it natively to use this.',
		'no-display': 'No desktop display detected on this host.',
		'no-monitors': 'No monitors detected on this host.',
		'missing-deps': 'The mss/pynput packages are not installed on the server.',
		'env-disabled': 'Disabled by the COMPUTER_USE_DISABLED environment variable.',
		'screenshot-probe-failed': 'The screen-capture probe failed on this host.'
	};

	const unavailableText = $derived(
		UNAVAILABLE_REASONS[computerUse.reason] ?? `Unavailable (${computerUse.reason}).`
	);

	const statusText = $derived(
		computerUse.active
			? `Controlling ${computerUse.displayLabel ?? 'your screen'}`
			: computerUse.enabled
				? 'On — available to every chat'
				: 'Off'
	);

	async function toggleComputerUse() {
		if (toggleBusy || !computerUse.available) return;
		toggleBusy = true;
		try {
			await computerUse.setEnabled(!computerUse.enabled);
		} finally {
			toggleBusy = false;
		}
	}
</script>

<!-- Computer use: one GLOBAL toggle for the whole app, living here rather
	not in the agent editor: it grants the same host hardware to every chat,
	so it is an input setting, not agent configuration. -->
<div class="set-group" data-testid="computer-use-section">
	<div class="set-grouphead">
		<h3><Monitor />Computer use</h3>
		<p>The purple frame identifies the active screen. The Agent pointer stays visible between actions.</p>
	</div>
	<div class="set-row" class:disabled={!computerUse.available}>
		<div class="lab">
			<h4>Enable computer use <span class="hint">{statusText}</span></h4>
			<p>
				Let chats control this machine's screen — screenshots, mouse, and keyboard,
				with a visible agent pointer. Press Esc on the host or Stop computer use to take control.
				Mouse and keyboard input are shared. Use browser tools for web tasks without moving
				your mouse; independent desktop input requires a separate desktop session or VM.
				Native (non-Docker) hosts only.
				{#if !computerUse.available}
					<br /><strong>{unavailableText}</strong>
				{/if}
			</p>
		</div>
		<div class="ctl">
			<button
				type="button"
				class="switch"
				class:on={computerUse.enabled}
				role="switch"
				aria-checked={computerUse.enabled}
				aria-label="Toggle computer use"
				aria-disabled={!computerUse.available || toggleBusy}
				disabled={!computerUse.available || toggleBusy}
				onclick={toggleComputerUse}
			><i></i></button>
		</div>
	</div>
</div>

{#if chatPrefs}
	<div class="set-group">
		<div class="set-grouphead"><h3><MessageSquare />Reply suggestions</h3></div>
		<div class="set-row">
			<div class="lab"><h4>Suggest next reply</h4><p>In code chats, send the final assistant response to your configured generated-text model for a suggested reply. Uses an extra model call. Suggestions are never sent automatically.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.suggestNextResponse}
					role="switch" aria-checked={chatPrefs.suggestNextResponse} aria-label="Suggest next reply"
					onclick={() => chatPreferences.updatePreference('suggestNextResponse', !chatPrefs?.suggestNextResponse)}><i></i></button>
			</div>
		</div>
	</div>
	<!-- Reasoning & tools -->
	<div class="set-group">
		<div class="set-grouphead">
			<h3><Brain />Reasoning &amp; tools</h3>
			<p>How thinking blocks, tool results and todos appear in the transcript.</p>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Show thinking blocks</h4><p>Display Claude's reasoning process.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.showThinkingBlocks}
					role="switch" aria-checked={chatPrefs.showThinkingBlocks} aria-label="Show thinking blocks"
					onclick={() => chatPreferences.setShowThinkingBlocks(!chatPrefs?.showThinkingBlocks)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Expand thinking by default</h4><p>Thinking blocks start expanded.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.thinkingStartExpanded}
					disabled={!chatPrefs.showThinkingBlocks}
					role="switch" aria-checked={chatPrefs.thinkingStartExpanded} aria-label="Expand thinking by default"
					onclick={() => chatPreferences.setThinkingStartExpanded(!chatPrefs?.thinkingStartExpanded)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Show thinking summary</h4><p>Preview thinking content when collapsed.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.showThinkingSummary}
					disabled={!chatPrefs.showThinkingBlocks}
					role="switch" aria-checked={chatPrefs.showThinkingSummary} aria-label="Show thinking summary"
					onclick={() => chatPreferences.setShowThinkingSummary(!chatPrefs?.showThinkingSummary)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Expand tools by default</h4><p>Tool results start expanded.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.toolsStartExpanded}
					role="switch" aria-checked={chatPrefs.toolsStartExpanded} aria-label="Expand tools by default"
					onclick={() => chatPreferences.setToolsStartExpanded(!chatPrefs?.toolsStartExpanded)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Group tool calls</h4><p>Collapse consecutive tool calls into one expandable card.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.groupToolCalls}
					role="switch" aria-checked={chatPrefs.groupToolCalls} aria-label="Group tool calls"
					onclick={() => chatPreferences.setGroupToolCalls(!chatPrefs?.groupToolCalls)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Expand todo list by default</h4><p>Task list starts expanded.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.todosStartExpanded}
					role="switch" aria-checked={chatPrefs.todosStartExpanded} aria-label="Expand todo list by default"
					onclick={() => chatPreferences.setTodosStartExpanded(!chatPrefs?.todosStartExpanded)}><i></i></button>
			</div>
		</div>
	</div>

	<!-- Messages -->
	<div class="set-group">
		<div class="set-grouphead">
			<h3><MessageSquare />Messages</h3>
			<p>Scrolling, timestamps and message density.</p>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Auto-scroll on new messages</h4><p>Automatically scroll to new content.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.autoScrollOnNewMessage}
					role="switch" aria-checked={chatPrefs.autoScrollOnNewMessage} aria-label="Auto-scroll on new messages"
					onclick={() => chatPreferences.setAutoScrollOnNewMessage(!chatPrefs?.autoScrollOnNewMessage)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Show message timestamps</h4><p>Display the time on each message.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.showMessageTimestamps}
					role="switch" aria-checked={chatPrefs.showMessageTimestamps} aria-label="Show message timestamps"
					onclick={() => chatPreferences.setShowMessageTimestamps(!chatPrefs?.showMessageTimestamps)}><i></i></button>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Compact message view</h4><p>Reduce spacing between messages.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={chatPrefs.compactMessageView}
					role="switch" aria-checked={chatPrefs.compactMessageView} aria-label="Compact message view"
					onclick={() => chatPreferences.setCompactMessageView(!chatPrefs?.compactMessageView)}><i></i></button>
			</div>
		</div>
	</div>
{:else}
	<div class="set-group">
		<div class="set-row"><div class="lab"><p>Loading preferences…</p></div></div>
	</div>
{/if}

<style>
	.switch:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	/* Live status next to the toggle title — small, quiet, never competes
	   with the row's description. */
	.hint {
		font-size: 0.78em;
		font-weight: normal;
		opacity: 0.65;
		margin-left: 0.4em;
	}
</style>
