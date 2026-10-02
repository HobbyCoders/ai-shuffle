import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import UsageRowsFixture from './UsageRowsFixture.svelte';
mount(UsageRowsFixture, { target: document.getElementById('app')! });
