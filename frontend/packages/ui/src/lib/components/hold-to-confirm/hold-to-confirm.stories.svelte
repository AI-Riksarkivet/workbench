<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import HoldToConfirm from './hold-to-confirm.svelte';

	const { Story } = defineMeta({
		title: 'Components/HoldToConfirm',
		component: HoldToConfirm,
		tags: ['autodocs'],
		args: {
			label: 'Clear',
			armedLabel: 'Press again',
			confirmedLabel: 'Cleared',
			variant: 'destructive',
			size: 'default',
			duration: 900,
			onConfirm: () => undefined,
		},
		argTypes: {
			variant: { control: 'select', options: ['default', 'destructive'] },
			size: { control: 'select', options: ['xs', 'sm', 'default'] },
			disabled: { control: 'boolean' },
		},
	});
</script>

<Story name="Destructive" />

<Story name="Default" args={{ variant: 'default', label: 'Revoke', confirmedLabel: 'Revoked' }} />

<Story name="Small" args={{ size: 'xs' }} />
