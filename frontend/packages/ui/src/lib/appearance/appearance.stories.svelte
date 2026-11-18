<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import { Button } from '../components/button/index.js';
	import AccentDialog from './accent-dialog.svelte';
	import AppearanceMenu from './appearance-menu.svelte';

	const { Story } = defineMeta({ title: 'Appearance/Picker', tags: ['autodocs'] });
</script>

<script lang="ts">
	let accentOpen = $state(false);
</script>

<Story name="Menu and accent dialog">
	<DropdownMenu.Root>
		<DropdownMenu.Trigger>
			{#snippet child({ props })}
				<Button {...props} variant="outline">Appearance</Button>
			{/snippet}
		</DropdownMenu.Trigger>
		<DropdownMenu.Content class="min-w-64" align="start">
			<AppearanceMenu oncustom={() => (accentOpen = true)} />
		</DropdownMenu.Content>
	</DropdownMenu.Root>
	<AccentDialog bind:open={accentOpen} />
</Story>
