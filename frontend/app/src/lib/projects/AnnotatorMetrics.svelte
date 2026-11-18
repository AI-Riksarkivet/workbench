<script lang="ts">
	// Who is doing the work, and how it is landing: each person's part over every item of the project, counted by the
	// annotator (`include=contributors`), never from the one page the browser holds.
	import { Badge } from '@workbench/ui/badge';

	import PersonName from './PersonName.svelte';
	import type { Contributor, People } from './types.ts';

	let { rows, people = {} }: { rows: readonly Contributor[]; people?: People } = $props();

	/** Nothing submitted is no rate, not a rate of zero, which would read as "everything they did was rejected". */
	const rate = (row: Contributor): string =>
		row.submitted === 0 ? '—' : `${Math.round((row.accepted / row.submitted) * 100)}%`;
</script>

{#if rows.length > 0}
	<section class="flex flex-col gap-2" data-testid="annotator-metrics">
		<h2 class="text-muted-foreground text-xs font-medium uppercase">People</h2>
		<div class="overflow-x-auto">
			<table class="w-full min-w-[32rem] text-sm">
				<thead>
					<tr class="text-muted-foreground border-b text-xs">
						<th class="py-1 text-left font-medium">Who</th>
						<th class="py-1 text-right font-medium" title="Items they hold, not yet submitted"
							>Open</th
						>
						<th class="py-1 text-right font-medium">Submitted</th>
						<th class="py-1 text-right font-medium">Accepted</th>
						<th class="py-1 text-right font-medium" title="Sent back for changes">Changes</th>
						<th class="py-1 text-right font-medium">Accept rate</th>
						<th class="py-1 text-right font-medium">Reviewed</th>
					</tr>
				</thead>
				<tbody>
					{#each rows as row (row.principal)}
						<tr class="border-b last:border-0" data-testid="metrics-row">
							<td class="py-1 text-xs"><PersonName principal={row.principal} {people} /></td>
							<td class="py-1 text-right tabular-nums">
								{#if row.open > 0}
									<Badge variant="outline">{row.open}</Badge>
								{:else}
									<span class="text-muted-foreground">0</span>
								{/if}
							</td>
							<td class="py-1 text-right tabular-nums">{row.submitted}</td>
							<td class="py-1 text-right tabular-nums">{row.accepted}</td>
							<td class="py-1 text-right tabular-nums">{row.changes_requested}</td>
							<td class="py-1 text-right tabular-nums" data-testid="accept-rate">{rate(row)}</td>
							<td class="py-1 text-right tabular-nums">{row.reviewed}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>
{/if}
