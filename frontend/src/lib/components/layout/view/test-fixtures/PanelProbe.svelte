<script lang="ts">
	import { getContext } from 'svelte';
	import { FEATURE_PANEL_CONTEXT, type FeaturePanelContext } from '../../feature-panel-context';
	import Switch from '../../../settings/rows/Switch.svelte';
	const feature = getContext<FeaturePanelContext>(FEATURE_PANEL_CONTEXT);
</script>
<span data-probe-active={feature.active}>Context arrived</span>
<Switch checked={false} />
