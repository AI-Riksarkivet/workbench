<script lang="ts">
	// A small labelled chip — match reasons, tags, statuses. Same API @workbench/ui's Chip exposed
	// (label + tone), design-token styled so it themes with the rest of @workbench/ui.
	let { label, tone = 'neutral' }: { label: string; tone?: 'neutral' | 'accent' } = $props();
</script>

<span
	data-slot="chip"
	class={[
		'inline-block px-1.5 py-0.5 text-[0.7rem] leading-tight whitespace-nowrap',
		tone === 'accent' ? 'bg-primary/14 text-foreground' : 'bg-muted text-muted-foreground',
	]}
>
	{label}
</span>
