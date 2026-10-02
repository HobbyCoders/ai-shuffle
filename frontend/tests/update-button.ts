import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import UpdateButtonFixture from './UpdateButtonFixture.svelte';
mount(UpdateButtonFixture, { target: document.getElementById('app')! });
