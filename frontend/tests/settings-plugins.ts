// Mounts the real Settings card the way a "Manage plugins" deep link opens it
// (#1610): `?tab=plugins` is MainPanel's requestedTab, `?item=` its item id.
// verify-settings-plugins.mjs answers the API, including /auth/status (admin).
import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';

const params = new URLSearchParams(location.search);
// The app stores import each other in a cycle; load them in the app's order
// (active-session pulls in app-data first) before the card.
await import('../src/lib/stores/active-session');
const [{ auth }, { default: SettingsCard }] = await Promise.all([
	import('../src/lib/stores/auth.svelte'),
	import('../src/lib/components/deck/cards/SettingsCard.svelte')
]);
await auth.checkAuth().catch(() => {});
mount(SettingsCard, {
	target: document.getElementById('app')!,
	props: { requestedTab: params.get('tab') ?? undefined, requestedItemId: params.get('item') }
});
