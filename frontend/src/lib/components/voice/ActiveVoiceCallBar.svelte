<script lang="ts">
	import { onMount } from 'svelte';
	import { Mic, MicOff, PhoneOff, Volume2, VolumeX, ArrowUpRight } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { layout } from '$lib/stores/layout.svelte';
	import { sessions } from '$lib/stores/app-data.svelte';
	import NotificationSlot from '$lib/components/NotificationSlot.svelte';
	import { DOCK_ORDER } from '$lib/stores/notification-dock.svelte';
	import { voiceCallState, isCallSurfaceActive, toggleMic, toggleAgent, hangup, reconcileVoiceCall, MIC_PROBLEM_LABEL } from '$lib/services/voiceCall';

	let { onReturn }: { onReturn: (sessionId: string) => void } = $props();
	const call = $derived($voiceCallState);
	const owner = $derived($sessions.find(s => s.id === call.sessionId));
	const micProblem = $derived(call.status === 'live' && !call.micMuted && call.micProblem);
	const visible = $derived(isCallSurfaceActive(call, call.sessionId) &&
		($activeSession.sessionId !== call.sessionId || $layout.activeView?.type !== 'session'));

	onMount(() => {
		const reconcile = () => {
			if (document.visibilityState === 'visible') void reconcileVoiceCall();
		};
		document.addEventListener('visibilitychange', reconcile);
		window.addEventListener('pageshow', reconcile);
		return () => {
			document.removeEventListener('visibilitychange', reconcile);
			window.removeEventListener('pageshow', reconcile);
		};
	});
</script>

{#if visible && call.sessionId}
	<NotificationSlot order={DOCK_ORDER.voiceCall}>
	<aside class="active-call" aria-label="Active voice call" data-testid="active-voice-call-bar">
		<button class="return" onclick={() => call.sessionId && onReturn(call.sessionId)} title="Return to voice conversation">
			<span><strong class:mic-problem={micProblem}>{micProblem ? MIC_PROBLEM_LABEL : `Voice call · ${call.status}`}</strong><span class="owner">{owner?.title || 'Voice session'}</span></span>
			<ArrowUpRight size={16} />
		</button>
		<button aria-label={call.micMuted ? 'Unmute microphone' : 'Mute microphone'} aria-pressed={call.micMuted} onclick={toggleMic}>
			{#if call.micMuted}<MicOff size={18} />{:else}<Mic size={18} />{/if}
		</button>
		<button aria-label={call.agentMuted ? 'Unmute speaker' : 'Mute speaker'} aria-pressed={call.agentMuted} onclick={toggleAgent}>
			{#if call.agentMuted}<VolumeX size={18} />{:else}<Volume2 size={18} />{/if}
		</button>
		<button class="end" aria-label="End voice call" onclick={() => hangup()}><PhoneOff size={18} /></button>
	</aside>
	</NotificationSlot>
{/if}

<style>
	/* Placed by the notification dock (#1823): it used to sit 12px from the window top, inside the desktop title bar's drag strip. */
	.active-call { display: flex; align-items: center; gap: 4px; max-width: 100%; padding: 6px; border: 1px solid var(--primary); border-radius: var(--radius); background: var(--background); color: var(--foreground); box-shadow: var(--shadow-l); }
	button { display: flex; align-items: center; justify-content: center; flex-shrink: 0; min-width: 40px; min-height: 40px; padding: 8px; border: 0; border-radius: var(--radius); background: transparent; color: inherit; cursor: pointer; }
	button:hover, button[aria-pressed='true'] { background: var(--muted); }
	button:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
	.return { gap: 12px; min-width: 0; text-align: left; }
	.return > span { min-width: 0; }
	strong, .owner { display: block; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 260px; }
	.owner { color: var(--muted-foreground); max-width: min(260px, 35vw); }
	.end { color: var(--destructive); }
	.mic-problem { color: var(--warning); }
</style>
