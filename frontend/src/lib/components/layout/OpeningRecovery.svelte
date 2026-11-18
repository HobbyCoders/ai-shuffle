<script lang="ts">
	import { activeSession } from '$lib/stores/active-session';
	import { getInitGeneration } from '$lib/stores/session/session-init';
	import { admittedOpening, finishOpening, pendingOpening, recoveringOpening, type Opening } from '$lib/stores/session/worktree-opening';
	import { openSession } from '$lib/services/session-navigation';

	let { note, ready, adopt }: { note: Opening; ready: boolean; adopt?: (id: string) => void } = $props();
	let unknown = $state(false);
	let check = $state(0);
	function current() {
		const place = activeSession.current;
		return pendingOpening()?.id === note.id && getInitGeneration() === note.generation && !place.sessionId
			&& place.project === note.project && place.projectV2Id === note.shared;
	}
	$effect(() => {
		if (!ready) return;
		void check;
		let cancelled = false;
		let timer: ReturnType<typeof setTimeout>;
		let rounds = 0;
		async function reconcile() {
			if (!current()) { recoveringOpening.set(null); return; }
			const id = await admittedOpening(note);
			if (cancelled || !current()) return;
			if (id) {
				const attemptId = note.id; // Adopting changes the view and invalidates these props.
				if (adopt) adopt(id);
				else openSession(id);
				finishOpening(attemptId);
				recoveringOpening.set(null);
			} else {
				unknown = true;
				if (++rounds < 4) timer = setTimeout(reconcile, 4000 * rounds);
			}
		}
		void reconcile();
		return () => { cancelled = true; clearTimeout(timer); };
	});
</script>

<section aria-label="Opening message" class="opening">
	<p role="status">{unknown ? 'Delivery not confirmed' : 'Checking your opening message…'}</p>
	{#if unknown && ready}
		<p>Your message may already have been sent. These words are saved in this tab.</p>
		<pre>{note.prompt}</pre>
		<button type="button" onclick={() => check++}>Check delivery again</button>
	{/if}
</section>

<style>
	.opening { padding: 1rem; max-width: 40rem; margin: auto; max-height: 100%; overflow: auto; }
	pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; }
</style>
