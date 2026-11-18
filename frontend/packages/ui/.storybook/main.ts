import type { StorybookConfig } from '@storybook/svelte-vite';

// @workbench/ui is a Svelte component *library* (svelte-package), not a SvelteKit app —
// so the framework is @storybook/svelte-vite, not @storybook/sveltekit.
const config: StorybookConfig = {
	stories: ['../src/**/*.stories.@(js|ts|svelte)'],
	addons: ['@storybook/addon-docs', '@storybook/addon-svelte-csf', '@storybook/addon-a11y'],
	// No build or dev server reports usage to Storybook: nothing about this project leaves the machine.
	core: { disableTelemetry: true },
	framework: {
		name: '@storybook/svelte-vite',
		options: {},
	},
};

export default config;
