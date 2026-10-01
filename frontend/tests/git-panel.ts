import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import GitPanelFixture from './GitPanelFixture.svelte';
// `?shell=mobile` renders the panel the way the Android build does: the mobile
// platform attribute and stylesheet, and buildShell() reporting mobile (see
// git-panel-stubs.ts). Without it the desktop shell is used at any width.
if (new URLSearchParams(location.search).get('shell') === 'mobile') {
	document.documentElement.setAttribute('data-platform', 'mobile');
	await import('../../mobile/src/mobile.css');
}
mount(GitPanelFixture, { target: document.getElementById('app')! });
