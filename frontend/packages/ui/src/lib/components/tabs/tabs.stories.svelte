<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import * as Tabs from './index.js';

	const { Story } = defineMeta({ title: 'Components/Tabs', tags: ['autodocs'] });
</script>

{#snippet strip(variant: 'line' | 'segment')}
	<Tabs.Root value="items" {variant} class="w-[28rem]">
		<Tabs.List aria-label="Project">
			<Tabs.Trigger value="items">Items</Tabs.Trigger>
			<Tabs.Trigger value="browse">Browse</Tabs.Trigger>
			<Tabs.Trigger value="review">Review</Tabs.Trigger>
			<Tabs.Trigger value="settings">Settings</Tabs.Trigger>
		</Tabs.List>
		<Tabs.Content value="items" class="text-muted-foreground text-sm">The items.</Tabs.Content>
		<Tabs.Content value="browse" class="text-muted-foreground text-sm"
			>Browse the items.</Tabs.Content
		>
		<Tabs.Content value="review" class="text-muted-foreground text-sm"
			>Review the items.</Tabs.Content
		>
		<Tabs.Content value="settings" class="text-muted-foreground text-sm"
			>The project's settings.</Tabs.Content
		>
	</Tabs.Root>
{/snippet}

<Story name="Line">
	{@render strip('line')}
</Story>

<Story name="Segment">
	{@render strip('segment')}
</Story>
