<script lang="ts">
	/**
	 * AccessScope: lifts read-only mode for its children while `writable`.
	 *
	 * For the rare control a viewer without owner rights may still use: the
	 * Android app's AI content report (Google Play asks that every user can
	 * report AI output). It only ever relaxes the access around it; everything
	 * outside the scope keeps the shell's read-only mode and approval banner.
	 */
	import type { Snippet } from 'svelte';
	import { getSettingsAccess, setSettingsAccess } from '$lib/settings/access.svelte';

	interface Props {
		writable: boolean;
		children: Snippet;
	}

	let { writable, children }: Props = $props();

	const parent = getSettingsAccess();
	setSettingsAccess({
		get readOnly() {
			return parent.readOnly && !writable;
		}
	});
</script>

{@render children()}
