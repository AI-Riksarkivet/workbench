<script lang="ts">
	// Compare: another version of the item beside the reader's own, on the same viewer. A version is another
	// annotator's replica of a consensus item (the shapes of that task's own draft, the annotator's record of each
	// task's work, so replicas never mix) or the model's proposals among the item's rows. The other version's shapes are drawn with the item's (a picture's outlined in
	// their difference's colour, a recording's on lanes of their own, a text's dashed under the line) and listed here by
	// how they stand: only theirs, placed or classed otherwise, only the reader's, alike. "Take" puts their shape into
	// the item, in place of the reader's where the two are one shape differing; one undo takes it back.
	import { GitCompareArrows, X } from '@lucide/svelte';
	import { Button } from '@workbench/ui/button';
	import { cn } from '@workbench/ui/utils';
	import { fetchDraft } from '#lib/api/projects.api.ts';
	import { buildBatchTable } from '../engine/index.ts';
	import type { TaskDetail } from '#lib/projects/types.ts';
	import type { AnnotatorController } from '../controller.svelte.ts';
	import type { DiffEntry, DiffKind } from './compare.ts';
	import { pointSlice } from '../text/codepoints.ts';

	let {
		controller,
		replicas,
	}: {
		controller: AnnotatorController;
		/** The item's other replicas (its consensus group), whose rows may be compared. */
		replicas: readonly TaskDetail[];
	} = $props();

	let chosen = $state<string | null>(null);
	const proposals = $derived(
		controller.rows.filter((r) => r.status === 'prediction' && r.source.startsWith('model:'))
			.length,
	);
	const versions = $derived([
		// Kept while compared, after the last proposal was taken or rejected, so the comparison says it is done.
		...(proposals > 0 || chosen === 'proposals'
			? [{ id: 'proposals', name: 'Model proposals' }]
			: []),
		...replicas.map((r) => ({
			id: r.task_id,
			name: `${/-r(\d+)$/.exec(r.task_id)?.[0].slice(1) ?? r.task_id} · ${r.submitted_by ?? r.assignee ?? 'unclaimed'}`,
		})),
	]);

	let reading = $state(false);
	let failure = $state<string | null>(null);

	async function show(id: string | null): Promise<void> {
		chosen = id;
		failure = null;
		if (id === null) {
			controller.compare = null;
			return;
		}
		const version = versions.find((v) => v.id === id);
		if (!version) return;
		if (id === 'proposals') {
			controller.compare = { name: version.name, table: null };
			return;
		}
		const own = controller.table;
		if (own === null) return;
		reading = true;
		const read = await fetchDraft(id);
		reading = false;
		if (chosen !== id) return;
		if (!read.ok) {
			failure = read.detail;
			controller.compare = null;
			return;
		}
		// The draft's shapes as rows of the canvas's own columns, so the comparison, the drawing and a take read them as
		// any row; a replica with no draft yet has saved no work.
		const rows = (read.data?.shapes ?? []).map((shape) => ({
			...shape,
			id: String(shape['shape_id'] ?? ''),
			status: 'accepted',
		}));
		controller.compare = { name: version.name, table: buildBatchTable(own.schema, rows) };
	}

	const WORDS: Record<DiffKind, string> = {
		theirs: 'only theirs',
		changed: 'differs',
		mine: 'only yours',
		same: 'alike',
	};
	const DOT: Record<DiffKind, string> = {
		theirs: 'bg-amber-500',
		changed: 'bg-pink-600',
		mine: 'bg-sky-600',
		same: 'bg-muted-foreground/50',
	};
	const differences = $derived(controller.compareDiff.filter((e) => e.kind !== 'same'));
	const alike = $derived(controller.compareDiff.length - differences.length);

	function describe(e: DiffEntry): string {
		const s = e.theirs ?? e.mine;
		if (!s) return '';
		const what = s.label || 'no class';
		if (e.kind === 'changed' && e.mine && e.theirs && e.mine.label !== e.theirs.label)
			return `${e.mine.label || 'no class'} → ${e.theirs.label || 'no class'}`;
		if (s.span) return `${what}: “${pointSlice(s.span.text, s.span.start, s.span.end)}”`;
		if (s.time && s.shape === 'segment')
			return `${what} ${s.time.start.toFixed(1)}–${s.time.end.toFixed(1)} s`;
		return `${what} · ${s.shape}`;
	}
</script>

{#if versions.length > 0}
	<section
		class="border-border border-b px-3 py-2"
		aria-label="Compare versions"
		data-testid="compare-panel"
	>
		<div class="flex items-center gap-2">
			<GitCompareArrows class="text-muted-foreground size-3.5 shrink-0" />
			<label class="text-muted-foreground text-xs" for="compare-version">Compare with</label>
			<select
				id="compare-version"
				class="bg-background h-7 min-w-0 flex-1 rounded-md border px-1 text-xs"
				value={chosen ?? ''}
				onchange={(e) => void show(e.currentTarget.value || null)}
				data-testid="compare-version"
			>
				<option value="">nothing</option>
				{#each versions as v (v.id)}
					<option value={v.id}>{v.name}</option>
				{/each}
			</select>
			{#if chosen !== null}
				<Button
					variant="ghost"
					size="icon-xs"
					aria-label="Stop comparing"
					onclick={() => void show(null)}
				>
					<X class="size-3.5" />
				</Button>
			{/if}
		</div>
		{#if reading}
			<p class="text-muted-foreground mt-1 text-xs" role="status">Reading the version…</p>
		{:else if failure}
			<p class="text-destructive mt-1 text-xs" role="alert">
				The version could not be read: {failure}
			</p>
		{:else if controller.compare}
			<p class="text-muted-foreground mt-1 text-xs" role="status" data-testid="compare-summary">
				{differences.length} difference{differences.length === 1 ? '' : 's'} · {alike} alike
			</p>
			<ul
				class="mt-1 flex max-h-48 flex-col gap-0.5 overflow-y-auto"
				data-testid="compare-differences"
			>
				{#each differences as e, i (`${e.kind}:${e.mine?.index ?? ''}:${e.theirs?.index ?? ''}:${i}`)}
					<li class="flex items-center gap-2 text-xs" data-kind={e.kind}>
						<span class={cn('size-2 shrink-0 rounded-full', DOT[e.kind])} aria-hidden="true"></span>
						<button
							type="button"
							class="hover:bg-muted min-w-0 flex-1 truncate rounded px-1 text-left"
							disabled={e.mine === null}
							title={e.mine === null ? 'Only the other version has this' : 'Select yours'}
							onclick={() => e.mine && controller.select(e.mine.index)}
						>
							<span class="text-muted-foreground">{WORDS[e.kind]}:</span>
							{describe(e)}
						</button>
						{#if e.theirs !== null && controller.readOnly === null}
							<Button
								variant="outline"
								size="xs"
								onclick={() => controller.takeFromCompare(e)}
								data-testid="compare-take">Take</Button
							>
						{/if}
					</li>
				{:else}
					<li class="text-muted-foreground text-xs">The two versions are alike.</li>
				{/each}
			</ul>
		{/if}
	</section>
{/if}
