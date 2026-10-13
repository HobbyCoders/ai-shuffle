import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import NotificationDockFixture from './NotificationDockFixture.svelte';

// ?shell=mobile stamps the phone shell and loads its stylesheet, as the mobile
// build does, so the phone-only touch-target rules are measured too.
if (new URLSearchParams(location.search).get('shell') === 'mobile') {
	document.documentElement.dataset.platform = 'mobile';
	await import('../../mobile/src/mobile.css');
}
mount(NotificationDockFixture, { target: document.getElementById('app')! });
