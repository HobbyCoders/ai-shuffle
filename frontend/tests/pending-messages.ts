import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import { theme } from '../src/lib/stores/theme.svelte';
import PendingMessagesFixture from './PendingMessagesFixture.svelte';
// ?theme=light|dark (default dark).
const params = new URLSearchParams(location.search);
theme.setTheme(params.get('theme') === 'light' ? 'light' : 'dark');
mount(PendingMessagesFixture, { target: document.getElementById('app')! });
