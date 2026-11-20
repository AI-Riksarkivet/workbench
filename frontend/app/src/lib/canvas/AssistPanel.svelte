<script lang="ts">
	// Assist: ask the task's models for proposals on the item. The annotator answers which models exist, whether each is
	// a real backend or its own stand-in, what each returns and whether that fits the task; the panel offers the ones a
	// person can ask from the canvas. A text-prompted detector is asked with its prompt (over the whole picture, or over a
	// box drawn while it is armed); a segmenter is armed, and each click on the picture is a point of its session ("this",
	// or with − "not this"), every answer replacing the last until Done. Proposals arrive as `prediction` rows for the
	// person to keep (A) or reject (R), as any proposal. A picture, or a video's frame, is what they look at: on a text or
	// a recording the panel says so instead.
	import { Check, Minus, Plus, Sparkles, X } from '@lucide/svelte';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import * as Popover from '@riksarkivet/ui/popover';
	import { Slider } from '@riksarkivet/ui/slider';
	import { cn } from '@riksarkivet/ui/utils';
	import { fetchProducers, type Producer } from '#lib/api/assist.api.ts';
	import type { AnnotatorController } from './controller.svelte.ts';

	let {
		controller,
		kind,
	}: { controller: AnnotatorController; kind: 'image' | 'audio' | 'video' | 'text' } = $props();

	let open = $state(false);
	let producers = $state.raw<Producer[] | null>(null);
	let failure = $state<string | null>(null);
	const visual = $derived(kind === 'image' || kind === 'video');
	const SignIcon = $derived(controller.assistPointPositive ? Plus : Minus);

	async function read(): Promise<void> {
		const unit = controller.unit;
		if (unit === null || producers !== null) return;
		const answer = await fetchProducers(unit);
		if (answer.ok) producers = answer.data.producers.filter((p) => p.interactive !== false);
		else failure = answer.detail;
	}

	const prompted = (p: Producer): boolean =>
		p.inputs.includes('prompt') || (p.inputs.length === 0 && p.name.startsWith('grounding-dino'));
	const clicked = (p: Producer): boolean =>
		p.inputs.includes('points') || (p.inputs.length === 0 && p.name.startsWith('sam'));
	const offered = $derived((producers ?? []).filter((p) => prompted(p) || clicked(p)));
	const mocked = $derived(producers !== null && !producers.some((p) => p.configured));

	function ask(p: Producer): void {
		controller.setAssistProducer(null);
		void controller.assist({ producer: p.name });
		open = false;
	}

	function arm(p: Producer): void {
		controller.setAssistProducer(p.name);
		open = false;
	}
</script>

<div class="flex shrink-0 items-center gap-1" data-testid="assist">
	<Popover.Root bind:open onOpenChange={(o) => o && void read()}>
		<Popover.Trigger>
			{#snippet child({ props })}
				<Button
					{...props}
					variant={controller.assistProducer ? 'secondary' : 'ghost'}
					size="sm"
					disabled={controller.readOnly !== null}
					data-testid="assist-open"
				>
					<Sparkles class="size-4" /> Assist
				</Button>
			{/snippet}
		</Popover.Trigger>
		<Popover.Content class="flex w-80 flex-col gap-3 p-3" data-testid="assist-panel">
			<div class="flex items-center justify-between gap-2">
				<h3 class="text-sm font-medium">Ask a model for proposals</h3>
				{#if mocked}
					<Badge
						variant="outline"
						title="No model server is configured: the annotator answers with its own stand-in"
						>stand-in</Badge
					>
				{/if}
			</div>
			{#if !visual}
				<p class="text-muted-foreground text-xs" data-testid="assist-not-here">
					The task's models propose regions of a picture or a video's frame; this item is {kind ===
					'text'
						? 'a text'
						: 'a recording'}, so there is nothing for them to look at here.
				</p>
			{:else if failure}
				<p class="text-destructive text-xs" role="alert">
					The models could not be listed: {failure}
				</p>
			{:else if producers === null}
				<p class="text-muted-foreground text-xs" role="status">Reading the task's models…</p>
			{:else if offered.length === 0}
				<p class="text-muted-foreground text-xs">
					The annotator offers no model to ask from the canvas.
				</p>
			{:else}
				<label class="flex flex-col gap-1 text-xs">
					<span class="text-muted-foreground">What to find (a detector's prompt)</span>
					<input
						class="bg-background h-8 rounded-md border px-2 text-sm"
						bind:value={controller.assistPrompt}
						placeholder="e.g. stamp"
						data-testid="assist-prompt"
						onkeydown={(e) => e.stopPropagation()}
					/>
				</label>
				<ul class="flex flex-col gap-1.5">
					{#each offered as p (p.name)}
						<li class="flex items-center gap-2 text-xs" data-testid="assist-producer">
							<span
								class={cn(
									'size-1.5 shrink-0 rounded-full',
									p.configured ? 'bg-emerald-500' : 'bg-amber-500',
								)}
								title={p.configured ? 'a configured model server' : "the annotator's stand-in"}
							></span>
							<span class="min-w-0 flex-1 truncate font-medium">{p.name}</span>
							<span class="text-muted-foreground">{p.returns.join(', ') || '?'}</span>
							{#if p.compatible === false}
								<Badge variant="destructive" class="text-[10px]">not this task's</Badge>
							{/if}
							{#if prompted(p)}
								<Button
									size="xs"
									variant="outline"
									disabled={!controller.assistPrompt.trim() || controller.assistBusy}
									onclick={() => ask(p)}>Find</Button
								>
							{/if}
							<Button size="xs" variant="outline" onclick={() => arm(p)} data-testid="assist-arm"
								>{clicked(p) ? 'Click' : 'Box'}</Button
							>
						</li>
					{/each}
				</ul>
				<label class="flex flex-col gap-1 text-xs">
					<span class="text-muted-foreground"
						>Keep proposals scored at least {controller.assistMinConfidence.toFixed(2)}</span
					>
					<Slider
						min={0}
						max={1}
						step={0.05}
						value={controller.assistMinConfidence}
						aria-label="The lowest score a proposal is kept at"
						onValueChange={(value) => (controller.assistMinConfidence = value)}
					/>
				</label>
			{/if}
		</Popover.Content>
	</Popover.Root>
	{#if controller.assistProducer}
		<!-- The armed model, its session's sign and its end, on the bar while the canvas takes its clicks. -->
		<span
			class="bg-secondary flex items-center gap-1 rounded-md px-2 py-0.5 text-xs"
			data-testid="assist-armed"
		>
			<Sparkles class="size-3" />
			{controller.assistProducer}{controller.assistPoints.length > 0
				? ` · ${controller.assistPoints.length} point${controller.assistPoints.length === 1 ? '' : 's'}`
				: ''}
			{#if controller.assistProducer.startsWith('sam')}
				<Button
					variant="ghost"
					size="icon-xs"
					aria-pressed={!controller.assistPointPositive}
					aria-label={controller.assistPointPositive
						? 'The next click says “this”: switch to “not this”'
						: 'The next click says “not this”: switch to “this”'}
					onclick={() => (controller.assistPointPositive = !controller.assistPointPositive)}
					data-testid="assist-sign"
				>
					<SignIcon class="size-3" />
				</Button>
			{/if}
			<Button
				variant="ghost"
				size="icon-xs"
				aria-label="Done: keep this answer"
				onclick={() => controller.finishAssistSession()}
				data-testid="assist-done"><Check class="size-3" /></Button
			>
			<Button
				variant="ghost"
				size="icon-xs"
				aria-label="Put the model away"
				onclick={() => controller.setAssistProducer(null)}
				data-testid="assist-disarm"><X class="size-3" /></Button
			>
		</span>
	{/if}
</div>
