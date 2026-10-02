<script lang="ts">
	import ChatInput from '../src/lib/components/chat/ChatInput.svelte';
	import SettingRow from '../src/lib/components/settings/rows/SettingRow.svelte';
	import Switch from '../src/lib/components/settings/rows/Switch.svelte';
	import '../src/lib/components/settings/shell/settings.css';
	import { chatPreferences } from '../src/lib/stores/chatPreferences.svelte';
</script>

<main>
	<h1>Reply suggestions</h1>
	<p>Isolated fixture · mocked transports · no model calls</p>
	<section class="composer"><ChatInput /></section>
	<section class="settings settings-shell">
		<SettingRow id="general.suggestReply">
			<Switch
				checked={$chatPreferences.suggestNextResponse}
				onchange={(on) => chatPreferences.updatePreference('suggestNextResponse', on)}
			/>
		</SettingRow>
	</section>
</main>

<style>
	main {
		max-width: 900px;
		margin: auto;
		padding: 24px 12px;
	}

	h1 {
		font-size: 24px;
		font-weight: 600;
	}

	p {
		color: var(--muted-foreground);
		margin: 8px 0 32px;
		font-size: 13px;
	}

	.composer {
		min-height: 180px;
	}

	.settings {
		display: block;
		padding: 12px;
	}

	:global(button:focus-visible) {
		outline: 2px solid var(--ring);
		outline-offset: 3px;
	}
</style>
