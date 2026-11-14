<script lang="ts">
	/**
	 * Which columns a table shows, behind one "Columns" button, so the chooser takes no row of the
	 * panel. Each column is a toggle button (`aria-pressed`); `children` adds a setting below them.
	 */
	import type { Snippet } from 'svelte';
	import { Columns3 } from '@lucide/svelte';
	import { Popover } from 'bits-ui';
	import { Button, buttonVariants } from '@workbench/ui';

	type Props = {
		columns: readonly { key: string; label: string }[];
		shown: readonly string[];
		ontoggle: (key: string) => void;
		children?: Snippet;
	};
	let { columns, shown, ontoggle, children }: Props = $props();
</script>

<Popover.Root>
	<Popover.Trigger
		class={buttonVariants({ variant: 'outline', size: 'sm' })}
		title="Choose the table's columns"
	>
		<Columns3 />
		<span class="sr-only @2xl:not-sr-only">Columns</span>
	</Popover.Trigger>
	<Popover.Portal>
		<Popover.Content
			sideOffset={6}
			align="end"
			class="border-border bg-popover text-popover-foreground z-50 flex w-64 flex-col gap-2 rounded-lg border p-2 text-xs shadow-md"
		>
			<div class="flex flex-wrap gap-1" role="group" aria-label="Columns shown">
				{#each columns as c (c.key)}
					{@const on = shown.includes(c.key)}
					<Button
						variant={on ? 'secondary' : 'ghost'}
						size="xs"
						class={on ? '' : 'text-muted-foreground'}
						aria-pressed={on}
						onclick={() => ontoggle(c.key)}
					>
						{c.label}
					</Button>
				{/each}
			</div>
			{@render children?.()}
		</Popover.Content>
	</Popover.Portal>
</Popover.Root>
