import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import CrumbStripFixture from './CrumbStripFixture.svelte';
import { auth } from '../src/lib/stores/auth.svelte';
// Match the app bootstrap: title actions fail closed until identity is known.
await auth.checkAuth();
mount(CrumbStripFixture, { target: document.getElementById('app')! });
