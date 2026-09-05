<script lang="ts">
	import BrowserPanel from '../src/lib/components/browser/BrowserPanel.svelte';
	import { activeSession } from '../src/lib/stores/active-session';
</script>

<BrowserPanel sessionId={$activeSession.sessionId ?? ''} />
