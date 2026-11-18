<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import BlockMark from './block-mark.svelte';

	const { Story } = defineMeta({
		title: 'Components/BlockMark',
		component: BlockMark,
		tags: ['autodocs'],
		args: { blocks: 5, size: 'md' },
		argTypes: {
			blocks: { control: 'select', options: [1, 2, 3, 4, 5] },
			size: { control: 'select', options: ['sm', 'md', 'lg'] },
		},
	});
</script>

<Story name="Mark">
	{#snippet template(args)}
		<BlockMark {...args} />
	{/snippet}
</Story>

<Story name="Beside the product's name">
	{#snippet template()}
		<span class="flex items-end gap-2">
			<BlockMark />
			<span class="text-[0.75rem] leading-none uppercase">workbench</span>
		</span>
	{/snippet}
</Story>
