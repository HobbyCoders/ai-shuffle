import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import ChatAccessFixture from './ChatAccessFixture.svelte';
mount(ChatAccessFixture, { target: document.getElementById('app')! });
