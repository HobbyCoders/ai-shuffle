<script lang="ts">
	/**
	 * The composer's usage-limit banner (#1831). Stopped by a limit: says when
	 * it resets and offers "Resume at reset" for this chat once, leaving the
	 * global switch alone. Armed: says when it resumes and offers Cancel.
	 *
	 * The worker's limit-resume state decides, so the banner comes back after a
	 * reload. It is read again whenever the chat's limit rows change, the
	 * owner sends, or a turn ends.
	 */
	import { untrack } from 'svelte';
	import Banner from '$lib/components/Banner.svelte';
	import {
		armLimitResume,
		cancelLimitResume,
		getSessionLimitResume,
		type SessionLimitResume
	} from '$lib/api/limit-resume';
	import { getErrorMessage } from '$lib/api/client';
	import { notify } from '$lib/stores/notifications';
	import type { ChatMessage } from '$lib/stores/types';
	import { LIMIT_RESUME_SUBTYPE, pausedChat } from './limit-resume';
	import { limitBanner } from './limit-banner';

	let {
		sessionId,
		messages,
		streaming = false,
		canAct = true
	}: {
		sessionId: string | null;
		messages: readonly ChatMessage[];
		streaming?: boolean;
		/** Arming, and cancelling a one-shot arm, are owner-only on the worker.
		 *  Anyone with the chat may cancel a resume the global switch scheduled. */
		canAct?: boolean;
	} = $props();

	// What should make us ask the worker again.
	const refetchKey = $derived.by(() => {
		if (!sessionId) return null;
		let lastRow = '';
		let lastOwner = '';
		for (let i = messages.length - 1; i >= 0 && (!lastRow || !lastOwner); i--) {
			const m = messages[i];
			if (!lastRow && m.systemSubtype === LIMIT_RESUME_SUBTYPE) lastRow = m.id;
			if (!lastOwner && m.role === 'user' && m.type !== 'system') lastOwner = m.id;
		}
		return `${sessionId}|${lastRow}|${lastOwner}|${streaming ? 1 : 0}`;
	});

	let limitState = $state<SessionLimitResume | null>(null);
	let busy = $state(false);
	let dismissed = $state<string | null>(null);
	let hiddenArm = $state<string | null>(null);
	let now = $state(Date.now() / 1000);
	let request = 0;

	async function load(sid: string) {
		const mine = ++request;
		try {
			const next = await getSessionLimitResume(sid);
			if (mine === request && sid === sessionId) limitState = next;
		} catch {
			// An older or unreachable worker: the transcript stands in.
			if (mine === request && sid === sessionId) limitState = null;
		}
	}

	// The key can flicker while a transcript reloads; ask once it settles, and
	// only when it settled somewhere new.
	const SETTLE_MS = 250;
	let fetchedKey: string | null = null;
	$effect(() => {
		const key = refetchKey;
		const sid = sessionId;
		if (!key || !sid) {
			limitState = null;
			fetchedKey = null;
			return;
		}
		untrack(() => {
			if (limitState && limitState.session_id !== sid) limitState = null;
		});
		const timer = setTimeout(() => {
			if (key === fetchedKey) return;
			fetchedKey = key;
			void load(sid);
		}, SETTLE_MS);
		return () => clearTimeout(timer);
	});

	$effect(() => {
		const t = setInterval(() => (now = Date.now() / 1000), 30_000);
		return () => clearInterval(t);
	});

	const transcript = $derived(sessionId ? pausedChat(messages) : null);
	const banner = $derived(
		sessionId ? limitBanner(limitState && limitState.session_id === sessionId ? limitState : null, transcript, now) : null
	);
	const armKey = $derived(banner?.kind === 'armed' ? `${sessionId}:${banner.resumeAt}` : null);

	async function arm() {
		if (!sessionId || banner?.kind !== 'stopped' || busy) return;
		const sid = sessionId;
		busy = true;
		try {
			const next = await armLimitResume(sid, banner.turnId);
			// The arm answer is newer than any read still in flight: drop those.
			request++;
			if (sid === sessionId) limitState = next;
		} catch (e) {
			notify(getErrorMessage(e, 'Could not schedule the resume.'), { severity: 'error' });
			void load(sid);
		} finally {
			busy = false;
		}
	}

	async function cancel() {
		if (!sessionId || !armKey || busy) return;
		const sid = sessionId;
		const key = armKey;
		busy = true;
		try {
			await cancelLimitResume(sid);
			hiddenArm = key;
			// load() drops every read that started before the cancel landed.
			void load(sid);
		} catch (e) {
			notify(getErrorMessage(e, 'Could not cancel the resume.'), { severity: 'error' });
		} finally {
			busy = false;
		}
	}
</script>

{#if banner?.kind === 'armed' && armKey !== hiddenArm}
	<Banner
		severity="info"
		message={banner.message}
		action={canAct || !banner.once ? { label: busy ? 'Cancelling…' : 'Cancel', onClick: cancel, disabled: busy } : undefined}
	/>
{:else if banner?.kind === 'stopped' && banner.key !== dismissed}
	<Banner
		severity="warning"
		message={banner.message}
		action={canAct && banner.canArm
			? { label: busy ? 'Scheduling…' : 'Resume at reset', onClick: arm, disabled: busy }
			: undefined}
		dismissible
		onDismiss={() => (dismissed = banner.key)}
	/>
{/if}
