<script lang="ts">
	/**
	 * The rows a maximized view's selection searched for (a Tree's topic, a Graph's entity): the dataset's
	 * search (`explore`, the same one the Results panel lists when the view is docked), listed and playable
	 * in place, as the page the maximized view stands in for listed them.
	 */
	import { ArrowLeft, X } from '@lucide/svelte';
	import HitList from '#lib/explore/components/hit-list.svelte';
	import PlayerPane from '#lib/explore/components/player-pane.svelte';
	import { explore } from '#lib/explore/explore.svelte.ts';
	import LoadFailure from '#lib/shell/load-failure.svelte';

	let {
		title,
		empty,
		onClose,
	}: {
		/** What the rows were searched for, the panel's heading. */
		title: string;
		/** What an empty answer says. */
		empty: string;
		onClose: () => void;
	} = $props();
	const id = $props.id();

	const hits = $derived(explore.hits);
	const active = $derived(explore.active);
</script>

<section class="bg-card flex h-full min-h-0 flex-col" aria-labelledby="rows-{id}">
	<header class="border-border flex h-11 shrink-0 items-center gap-2 border-b px-3">
		{#if active}
			<button
				type="button"
				onclick={() => (explore.active = null)}
				aria-label="Back to the rows"
				class="text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-ring/50 rounded-md p-1 transition-colors outline-none focus-visible:ring-3"
			>
				<ArrowLeft class="size-4" />
			</button>
		{/if}
		<h2 id="rows-{id}" class="text-foreground min-w-0 truncate text-sm font-medium" {title}>
			{title}
		</h2>
		{#if !explore.loadingHits && !explore.error}
			<span class="text-muted-foreground shrink-0 text-xs">
				· {hits.length}{explore.allLoaded ? '' : '+'}
				{hits.length === 1 ? 'row' : 'rows'}
			</span>
		{/if}
		<button
			type="button"
			onclick={onClose}
			aria-label="Close the rows"
			class="text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-ring/50 ml-auto shrink-0 rounded-md p-1 transition-colors outline-none focus-visible:ring-3"
		>
			<X class="size-4" />
		</button>
	</header>

	<div class="min-h-0 flex-1 overflow-y-auto">
		{#if active}
			<PlayerPane hit={active} />
		{:else if explore.error && !explore.loadingHits}
			<div class="flex justify-center">
				<LoadFailure
					what="The rows could not be loaded."
					detail={explore.error}
					onretry={() => void explore.refresh()}
				/>
			</div>
		{:else}
			<HitList
				{hits}
				{active}
				onselect={(h) => (explore.active = h)}
				emptyMessage={explore.loadingHits ? 'Reading the rows…' : empty}
			/>
		{/if}
	</div>
</section>
