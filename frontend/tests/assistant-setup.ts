import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import AssistantHome from '../src/lib/components/assistant/AssistantHome.svelte';

(window as unknown as { assistantFixture: { opened: unknown[] } }).assistantFixture = { opened: [] };
mount(AssistantHome, { target: document.getElementById('app')! });
