import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import ComputerUseBannerFixture from './ComputerUseBannerFixture.svelte';
mount(ComputerUseBannerFixture, { target: document.getElementById('app')! });
