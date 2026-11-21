<script lang="ts">
	/**
	 * The item the reader opened, in the player: its media through a signed link, its transcript,
	 * timeline and speakers where the dataset declares them.
	 */
	import PlayerPane from '#lib/explore/components/player-pane.svelte';
	import { explore } from '#lib/explore/explore.svelte.ts';
</script>

<section class="bg-muted/30 h-full min-h-0" aria-label="Player">
	<PlayerPane hit={explore.active} query={explore.spec.q} />
</section>
