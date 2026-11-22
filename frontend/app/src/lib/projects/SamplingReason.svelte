<script lang="ts">
	// Why an item stands where it does in a list the project's sampling orders (ADR 0034), the same on the Label tab, the
	// items table and the quick view: what placed it there (the model's doubt, or how unlike the labelled items it is),
	// that the model proposed nothing, or that it has no number yet, with who scored or measured it and when in a tooltip
	// and in the chip's accessible name, so it is read without a pointer (or as text, `detailed`, in the quick view); and
	// who chose the item to be sent: sampling or a person.
	import { badgeVariants } from '@riksarkivet/ui/badge';
	import * as Tooltip from '@riksarkivet/ui/tooltip';

	import { chosenLine, samplingChip, samplingDetail } from './sampling.ts';
	import type { Chosen, ItemSampling, SamplingStrategy } from './types.ts';

	let {
		sampling,
		strategy,
		chosen,
		detailed = false,
	}: {
		sampling: ItemSampling | null | undefined;
		strategy: SamplingStrategy;
		chosen?: Chosen | null;
		/** Say the full detail as text beneath the chip. */
		detailed?: boolean;
	} = $props();

	const chip = $derived(samplingChip(sampling, strategy));
	const detail = $derived(samplingDetail(sampling));
	const why = $derived(chosenLine(chosen));
</script>

{#if chip || why}
	<span class="flex min-w-0 flex-wrap items-center gap-1 text-[11px]">
		{#if chip && detail.length > 0}
			<Tooltip.Provider>
				<Tooltip.Root>
					<Tooltip.Trigger
						class={badgeVariants({ variant: 'outline' })}
						tabindex={-1}
						aria-label={`${chip.text}. ${detail.join('. ')}`}
						data-testid="sampling-reason">{chip.text}</Tooltip.Trigger
					>
					<Tooltip.Content data-testid="sampling-detail">
						<span class="flex flex-col gap-0.5">
							{#each detail as line (line)}
								<span>{line}</span>
							{/each}
						</span>
					</Tooltip.Content>
				</Tooltip.Root>
			</Tooltip.Provider>
		{:else if chip}
			<span
				class={chip.quiet ? 'text-muted-foreground' : badgeVariants({ variant: 'outline' })}
				data-testid="sampling-reason">{chip.text}</span
			>
		{/if}
		{#if why}
			<span class="text-muted-foreground" data-testid="sampling-chosen">{why}</span>
		{/if}
		{#if detailed && detail.length > 0}
			<span
				class="text-muted-foreground flex basis-full flex-col"
				data-testid="sampling-detail-text"
			>
				{#each detail as line (line)}
					<span>{line}</span>
				{/each}
			</span>
		{/if}
	</span>
{/if}
