import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';
import SupportReportFixture from './SupportReportFixture.svelte';
mount(SupportReportFixture, { target: document.getElementById('app')! });
