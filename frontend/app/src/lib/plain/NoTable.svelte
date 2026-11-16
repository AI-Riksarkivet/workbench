<script lang="ts">
	/**
	 * What a plain table's panel shows while it has no table to draw from (`#lib/plain/plain.svelte.ts`):
	 * the dataset still being read; a dataset with no table the viewer can read, with why each one could
	 * not be opened; or a table a view or a link named that the dataset does not hold, with the tables it
	 * does hold to pick from. Never a wait that cannot end.
	 */
	import { plain } from './plain.svelte.ts';

	const facts = $derived(plain.facts);
	const tables = $derived(Object.keys(facts?.tables ?? {}).sort());
	const unreadable = $derived(Object.entries(facts?.unreadable ?? {}));
</script>

{#if !facts}
	<p class="text-muted-foreground m-auto p-6 text-sm" role="status">Reading the dataset…</p>
{:else if tables.length === 0}
	<div class="m-auto flex max-w-sm flex-col gap-2 p-6 text-center" role="alert">
		<p class="text-sm font-medium">This dataset holds no table the viewer can read</p>
		{#each unreadable as [name, reason] (name)}
			<p class="text-muted-foreground text-xs">
				<span class="text-foreground font-mono">{name}</span> could not be opened: {reason}
			</p>
		{/each}
	</div>
{:else}
	<div class="m-auto flex max-w-sm flex-col items-center gap-2 p-6 text-center" role="alert">
		<p class="text-sm font-medium">
			{plain.missing
				? `This dataset holds no table ${plain.missing}`
				: 'No table of this dataset is chosen'}
		</p>
		<label class="flex items-center gap-1.5 text-xs">
			<span class="text-muted-foreground">Pick a table</span>
			<select
				class="border-input bg-background focus-visible:ring-ring/50 h-7 rounded-md border px-1.5 text-xs outline-none focus-visible:ring-3"
				value=""
				onchange={(e) => plain.choose(e.currentTarget.value)}
			>
				<option value="" disabled>Choose…</option>
				{#each tables as name (name)}
					<option value={name}>{name}</option>
				{/each}
			</select>
		</label>
	</div>
{/if}
