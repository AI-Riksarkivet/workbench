<script lang="ts">
	// What a reviewer sending back an item a judge flagged says of the flag: right, by default, since sending an item back
	// mostly agrees with it, so they change it only when it was wrong (ADR 0026). Said of every flagged item a send-back
	// covers at once.
	import { Checkbox } from '@riksarkivet/ui/checkbox';

	let { agreed = $bindable(true), count = 1 }: { agreed?: boolean; count?: number } = $props();
</script>

<label class="flex items-center gap-2 text-sm" data-testid="send-back-judge">
	<Checkbox bind:checked={agreed} />
	{count === 1 ? "The judge's flag was right" : `The judge's flags on ${count} items were right`}
</label>
