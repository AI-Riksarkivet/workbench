<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { Progress } from './index.js';

	const { Story } = defineMeta({
		title: 'Components/Progress',
		component: Progress,
		tags: ['autodocs'],
		args: { value: 7, max: 12, segments: 16, 'aria-label': 'Items finished' },
		argTypes: {
			value: { control: 'number' },
			max: { control: 'number' },
			segments: { control: 'number' },
		},
	});
</script>

<Story name="Block meter">
	{#snippet template(args)}
		<div class="w-72"><Progress {...args} /></div>
	{/snippet}
</Story>

<Story name="Full" args={{ value: 12 }}>
	{#snippet template(args)}
		<div class="w-72"><Progress {...args} /></div>
	{/snippet}
</Story>
