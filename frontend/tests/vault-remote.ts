import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import VaultFixture from './VaultFixture.svelte';
mount(VaultFixture, { target: document.getElementById('app')! });
