<script lang="ts">
	import { Dialog as BitsDialog, type WithoutChild } from 'bits-ui';
	import { cn } from '../../utils/cn.js';

	let {
		class: className,
		ref = $bindable(null),
		children,
		...rest
	}: WithoutChild<BitsDialog.ContentProps> = $props();
</script>

<BitsDialog.Portal>
	<BitsDialog.Overlay
		class="data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 fixed inset-0 z-50 bg-scrim"
	/>
	<BitsDialog.Content
		bind:ref
		class={cn(
			'bg-popover text-popover-foreground shadow-float fixed top-1/2 left-1/2 z-50 grid w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 gap-5 rounded-none p-7',
			'data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=open]:slide-in-from-bottom-1',
			className,
		)}
		{...rest}
	>
		{@render children?.()}
	</BitsDialog.Content>
</BitsDialog.Portal>
