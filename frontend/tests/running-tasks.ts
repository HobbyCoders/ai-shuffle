import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import presets from '../src/lib/themes/presets.json';
import RunningTasksFixture from './RunningTasksFixture.svelte';

// ?shell=mobile renders the way the Android build does (platform attribute,
// mobile stylesheet, buildShell() reporting mobile). ?theme=vercel applies that
// preset; ?mode=light switches the light palette.
const params = new URLSearchParams(location.search);
const root = document.documentElement;
const mode = params.get('mode') === 'light' ? 'light' : 'dark';
root.classList.toggle('dark', mode === 'dark');
root.classList.toggle('light', mode === 'light');
if (params.get('shell') === 'mobile') {
	root.setAttribute('data-platform', 'mobile');
	await import('../../mobile/src/mobile.css');
} else {
	root.setAttribute('data-platform', 'desktop');
}
const themeId = params.get('theme');
if (themeId) {
	const list = (Array.isArray(presets) ? presets : (presets as { presets?: unknown[] }).presets ?? []) as {
		id: string;
		styles: Record<'light' | 'dark', Record<string, string>>;
	}[];
	const preset = list.find((p) => p.id === themeId);
	if (preset) {
		root.setAttribute('data-theme-preset', preset.id);
		for (const [key, value] of Object.entries(preset.styles[mode])) root.style.setProperty(`--${key}`, value);
	}
}
mount(RunningTasksFixture, { target: document.getElementById('app')! });
