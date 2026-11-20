<script lang="ts" module>
	import type { Span } from './timeline.ts';

	/** A word of a transcript line, timed. */
	export type TranscriptWord = Span & { text: string };

	/** One line of a transcript: what was said from `start` to `end`, and its words when they are timed. */
	export type TranscriptLine = Span & {
		key: string;
		text: string;
		words?: readonly TranscriptWord[];
	};
</script>

<script lang="ts">
	// A recording's transcript, synced to its playhead: every line in order, the line being spoken marked and kept in
	// view, its word under the playhead marked, and a line pressed seeks there. `caption` is the fullscreen band: the
	// line being spoken, alone.
	//
	// The marks are derived from the time the holder passes (the element's own `currentTime`), so nothing here listens
	// to the element. The current word is the design system's primary pair (`bg-primary` under
	// `text-primary-foreground`), whose contrast the tokens hold in both themes.
	import { formatClock, spanAt } from './timeline.ts';
	import { cn } from '@riksarkivet/ui/utils';

	let {
		lines,
		currentTime,
		onseek,
		terms = new Set<string>(),
		variant = 'panel',
		empty = 'No transcript for this part.',
		class: className,
	}: {
		lines: readonly TranscriptLine[];
		currentTime: number;
		onseek: (t: number) => void;
		/** Lowercased words to mark as matches of the reader's search. */
		terms?: ReadonlySet<string>;
		variant?: 'panel' | 'caption';
		empty?: string;
		class?: string;
	} = $props();

	const current = $derived(spanAt(lines, currentTime));
	const currentLine = $derived(current >= 0 ? lines[current] : undefined);
	/** The word under the playhead, while the playhead is still within its line. */
	const currentWord = $derived(
		currentLine?.words && currentTime < currentLine.end
			? spanAt(currentLine.words, currentTime)
			: -1,
	);

	/** A word's text split around its spaces, so a mark covers the word and not the space before it. */
	function parts(text: string): { lead: string; core: string; trail: string } {
		const m = /^(\s*)([\s\S]*?)(\s*)$/.exec(text);
		return { lead: m?.[1] ?? '', core: m?.[2] ?? text, trail: m?.[3] ?? '' };
	}
	const normalize = (w: string) => w.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '').toLowerCase();

	// ── following the playhead ──
	// The line being spoken is scrolled into view inside this list (never the page around it), unless the reader
	// scrolled the list themselves in the last few seconds: then it waits, so it does not pull the text from under them.
	let scroller = $state<HTMLDivElement | null>(null);
	let readerScrolledAt = 0;
	const READER_HOLD_MS = 4000;
	const readerScrolled = () => (readerScrolledAt = performance.now());

	$effect(() => {
		const el = scroller;
		const i = current;
		if (el === null || i < 0 || variant !== 'panel') return;
		if (performance.now() - readerScrolledAt < READER_HOLD_MS) return;
		const line = el.querySelector<HTMLElement>(`[data-line="${i}"]`);
		if (!line) return;
		const top = line.offsetTop;
		const bottom = top + line.offsetHeight;
		if (top >= el.scrollTop && bottom <= el.scrollTop + el.clientHeight) return;
		el.scrollTo({ top: Math.max(0, top - el.clientHeight / 3), behavior: 'smooth' });
	});
</script>

{#snippet words(line: TranscriptLine, isCurrent: boolean)}
	{#if line.words && line.words.length > 0}
		{#each line.words as word, w (`${word.start}:${w}`)}
			{@const p = parts(word.text)}
			{p.lead}<span
				class={cn(
					'rounded-[3px]',
					isCurrent && w === currentWord && 'bg-primary text-primary-foreground',
					terms.has(normalize(p.core)) &&
						'decoration-highlight underline decoration-2 underline-offset-2',
				)}
				data-current-word={isCurrent && w === currentWord ? '' : undefined}>{p.core}</span
			>{p.trail}
		{/each}
	{:else}
		{line.text}
	{/if}
{/snippet}

{#if variant === 'caption'}
	{#if currentLine}
		<p class={cn('text-center text-lg leading-8 text-balance', className)} aria-live="off">
			{@render words(currentLine, true)}
		</p>
	{/if}
{:else}
	<div
		bind:this={scroller}
		class={cn('relative min-h-0 overflow-x-hidden overflow-y-auto', className)}
		role="region"
		aria-label="Transcript"
		onwheel={readerScrolled}
		ontouchmove={readerScrolled}
		data-testid="transcript"
	>
		{#if lines.length === 0}
			<p class="text-muted-foreground p-3 text-xs">{empty}</p>
		{:else}
			<ol class="flex flex-col gap-0.5 p-1.5">
				{#each lines as line, i (line.key)}
					{@const isCurrent = i === current}
					<li data-line={i}>
						<button
							type="button"
							class={cn(
								'focus-visible:ring-ring/50 grid w-full grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-3 rounded-md px-2 py-1.5 text-left text-sm leading-6 transition-colors outline-none focus-visible:ring-3',
								isCurrent
									? 'bg-primary/10 text-foreground'
									: 'text-muted-foreground hover:bg-muted',
							)}
							aria-current={isCurrent ? 'true' : undefined}
							title={`Play from ${formatClock(line.start)}`}
							onclick={() => onseek(line.start)}
						>
							<span
								class={cn(
									'font-mono text-xs tabular-nums',
									isCurrent ? 'text-primary font-medium' : 'text-muted-foreground',
								)}>{formatClock(line.start)}</span
							>
							<span class="min-w-0 break-words [overflow-wrap:anywhere] whitespace-normal">
								{@render words(line, isCurrent)}
							</span>
						</button>
					</li>
				{/each}
			</ol>
		{/if}
	</div>
{/if}
