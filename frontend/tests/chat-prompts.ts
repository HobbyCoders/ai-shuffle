import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';
import { theme } from '../src/lib/stores/theme.svelte';
import { setPlatformForMobileApp } from '../src/lib/stores/platform.svelte';
// The session store first, as in the app: app-data and the session switcher import each other.
import '../src/lib/stores/active-session';
import ChatPromptsFixture from './ChatPromptsFixture.svelte';
import { appData } from '../src/lib/stores/app-data.svelte';

/**
 * Browser review fixture for the in-chat question and confirmation cards and
 * the Attach a project sheet.
 *
 *   ?theme=light|dark     (default dark)
 *   ?shell=mobile         the phone shell: mobile.css and platform.isMobile,
 *                         as in the Android app's bundle
 *   ?only=existing|approvals|generation   one section
 *   ?sheet=open           open the attach sheet on load
 */
const params = new URLSearchParams(location.search);
theme.setTheme(params.get('theme') === 'light' ? 'light' : 'dark');
if (params.get('shell') === 'mobile') {
	setPlatformForMobileApp();
	await import('../../mobile/src/mobile.css');
}
await Promise.all([appData.loadProjects(), appData.loadClusterProjects()]);

mount(ChatPromptsFixture, {
	target: document.getElementById('app')!,
	props: { only: params.get('only'), sheetOpen: params.get('sheet') === 'open' }
});
