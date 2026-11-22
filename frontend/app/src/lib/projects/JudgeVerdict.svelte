<script lang="ts">
	// What a project's judge said of one submission, the same on the Review tab and on the canvas: a flagged item says
	// "Flagged" and each thing the judge disagreed with (a label, or the whole item), its reason and the class it would
	// have given instead; an item it found nothing in says so quietly. Either names the model and version that judged.
	// Where the reviewer has decided, whether they agreed with the flags. On the canvas a flagged label is a button
	// that selects it (`onshape`).
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';

	import type { Judgement } from './types.ts';

	let {
		judgement,
		shapeName = () => null,
		onshape,
	}: {
		judgement: Judgement;
		/** What a flagged label is called (its class), where the page holds the item's labels. */
		shapeName?: (shapeId: string) => string | null;
		/** Select a flagged label on the canvas. */
		onshape?: (shapeId: string) => void;
	} = $props();

	const by = $derived(`judged by ${judgement.model} ${judgement.version}`);
	const labelOf = (shapeId: string): string => {
		const name = shapeName(shapeId);
		return name ? `The ${name} label` : 'A label';
	};
</script>

{#if judgement.flags.length > 0}
	<div class="flex min-w-0 flex-col gap-1 text-xs" data-testid="judge-flagged">
		<div class="flex flex-wrap items-center gap-2">
			<Badge variant="destructive">Flagged</Badge>
			<span class="text-muted-foreground" data-testid="judge-by">{by}</span>
			{#if judgement.agreed === true}
				<span class="text-muted-foreground" data-testid="judge-agreed">· the reviewer agreed</span>
			{:else if judgement.agreed === false}
				<span class="text-muted-foreground" data-testid="judge-agreed"
					>· the reviewer disagreed</span
				>
			{/if}
		</div>
		<ul class="flex flex-col gap-0.5 pl-1">
			{#each judgement.flags as flag (`${flag.shape_id ?? 'item'}:${flag.reason}`)}
				<li class="break-words" data-testid="judge-flag">
					{#if flag.shape_id === null}
						<span class="font-medium">Whole item:</span>
					{:else if onshape}
						{@const shapeId = flag.shape_id}
						<Button
							variant="link"
							size="xs"
							class="h-auto px-0 font-medium"
							title="Select it on the canvas"
							data-testid="judge-flag-shape"
							onclick={() => onshape(shapeId)}>{labelOf(shapeId)}</Button
						>:
					{:else}
						<span class="font-medium">{labelOf(flag.shape_id)}:</span>
					{/if}
					{flag.reason}
					{#if flag.suggested_label}
						<span class="text-muted-foreground">(suggests {flag.suggested_label})</span>
					{/if}
				</li>
			{/each}
		</ul>
	</div>
{:else}
	<span class="text-muted-foreground text-xs" data-testid="judge-clear">Nothing flagged, {by}</span>
{/if}
