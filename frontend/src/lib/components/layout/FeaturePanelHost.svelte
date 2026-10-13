<script lang="ts">
	/**
	 * FeaturePanelHost — wraps one kept-alive feature panel and tells the page
	 * inside whether it is currently on screen (see feature-panel-context.ts).
	 */
	import { setContext, type Snippet } from 'svelte';
	import { FEATURE_PANEL_CONTEXT, type FeaturePanelContext } from './feature-panel-context';

	let { active, children }: { active: boolean; children: Snippet } = $props();

	setContext<FeaturePanelContext>(FEATURE_PANEL_CONTEXT, {
		get active() {
			return active;
		}
	});
</script>

{@render children()}
