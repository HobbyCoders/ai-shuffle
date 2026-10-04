import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import CrumbStripFixture from './CrumbStripFixture.svelte';
mount(CrumbStripFixture, { target: document.getElementById('app')! });
