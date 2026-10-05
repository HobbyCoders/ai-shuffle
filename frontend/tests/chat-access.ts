import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import { theme } from '../src/lib/stores/theme.svelte';
import ChatAccessFixture from './ChatAccessFixture.svelte';
// ?theme=light|dark (default dark) and ?preset=<theme preset id> let the
// screenshot script check the access UI across themes.
const params = new URLSearchParams(location.search);
theme.setTheme(params.get('theme') === 'light' ? 'light' : 'dark');
if (params.get('preset')) theme.setPreset(params.get('preset'));
mount(ChatAccessFixture, { target: document.getElementById('app')! });
