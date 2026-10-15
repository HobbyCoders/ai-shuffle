<script lang="ts">
	import { onDestroy } from 'svelte';
	import ChatView from '../src/lib/components/layout/ChatView.svelte';
	import { activeSession } from '../src/lib/stores/active-session';
	import { initialState, reduce, releaseMessages, type WireEvent } from '../src/lib/stores/session/v2/reducer';

	// Opt-in mixed native-event history for bounded mounting/browser regression.
	// The existing fixture transport answers all API calls locally.
	const params = new URLSearchParams(location.search);
	const mixed = params.has('mixed');
	const giant = params.has('giant');
	const deltaCount = Math.max(1, Number(params.get('deltas') || 100));
	let reduced = initialState();
	let seq = 0;
	let timer: ReturnType<typeof setInterval> | undefined;
	let streaming = $state(false);
	let delta = 0;
	function event(type: string, fields: Record<string, unknown> = {}) {
		reduced = reduce(reduced, { type, id: `fixture-${++seq}`, seq, session_id: 'fixture', ...fields } as WireEvent);
	}
	function publish() {
		Object.assign(activeSession.current, {
			messages: releaseMessages(reduced), isStreaming: reduced.isStreaming,
			liveTurnId: reduced.liveTurnId, isRemote: false
		});
		// The production store uses explicit subscriber publication, not a rune
		// proxy. Publish the mocked reducer fold through its no-I/O setter.
		activeSession.setScrollTop(activeSession.current.scrollTop);
	}
	if (mixed) {
		const count = giant ? 1 : Number(params.get('turns') || 500);
		for (let i = 0; i < count; i++) {
			const turn_id = `turn-${i}`;
			event('turn.started', { turn_id });
			event('message.user', { turn_id, id: `u${i}`, content: `Prompt ${i + 1}: mixed history marker ${i + 1}` });
			event('assistant.thinking_delta', { turn_id, text: `Considering request ${i + 1}.` });
			for (let tool = 0; tool < (giant ? 2000 : 1); tool++) {
				const tool_use_id = `tool-${i}-${tool}`;
				event('tool.use', { turn_id, tool_use_id, name: 'Read', input: { file_path: `example-${tool}.ts` } });
				event('tool.result', { turn_id, tool_use_id, content_preview: 'Example source loaded.', is_error: false });
			}
			event('assistant.text_delta', { turn_id, text: `Reply ${i + 1}. **Completed** the check.\n\n` + 'A mixed native-event transcript with tools and prose. '.repeat(5) });
			event('turn.completed', { turn_id });
		}
		publish();
	}
	function stream() {
		if (timer) return;
		streaming = true;
		delta = 0;
		const turn_id = `stream-${seq}`;
		event('turn.started', { turn_id });
		event('message.user', { turn_id, content: 'Live streaming fixture' });
		publish();
		timer = setInterval(() => {
			event('assistant.text_delta', { turn_id, text: `delta-${++delta} ` });
			if (delta === deltaCount) {
				event('turn.completed', { turn_id });
				clearInterval(timer);
				timer = undefined;
				streaming = false;
			}
			publish();
		}, 50);
	}
	onDestroy(() => clearInterval(timer));
</script>

<main>
	{#if mixed}
		<div class="fixture-controls">
			<span>Mixed native history · {activeSession.current.messages.length} rows</span>
			<button onclick={stream} disabled={streaming}>{streaming ? 'Streaming…' : `Stream ${deltaCount} deltas`}</button>
		</div>
	{/if}
	<ChatView />
</main>

<style>
	main { display: flex; flex-direction: column; height: 100dvh; overflow: hidden; }
	.fixture-controls { display: flex; gap: 12px; align-items: center; padding: 8px; font-size: 12px; }
	button { min-height: 44px; border: 1px solid var(--border); border-radius: 8px; padding: 8px; }
</style>
