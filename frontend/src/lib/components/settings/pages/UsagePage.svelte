<script lang="ts">
	/**
	 * Usage: Spending (the daily spending limit and job time limit) and Usage
	 * limits (resume after a provider's usage limit resets). One registry
	 * entry (entries/usage.ts) per `<SettingRow id>`.
	 */
	import PreferencePage from '../shell/PreferencePage.svelte';
	import SpendingRows from './SpendingRows.svelte';
	import LimitResumeRows from './LimitResumeRows.svelte';
</script>

<PreferencePage category="usage">
	<SpendingRows />
	<LimitResumeRows />
</PreferencePage>
