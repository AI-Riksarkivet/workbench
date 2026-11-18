<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import RollingDigits from './rolling-digits.svelte';

	const { Story } = defineMeta({
		title: 'Components/RollingDigits',
		component: RollingDigits,
		tags: ['autodocs'],
		args: { value: 128 },
		argTypes: { value: { control: 'number' }, pad: { control: 'number' } },
	});
</script>

<Story name="Count">
	{#snippet template(args)}
		<span class="text-3xl"><RollingDigits {...args} /></span>
	{/snippet}
</Story>

<Story
	name="Capped at 99+"
	args={{ value: 140, format: (n: number) => (n > 99 ? '99+' : String(n)) }}
/>
