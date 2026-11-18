<script lang="ts">
	// A person, by name where the annotator keeps one (`people.ts`), their principal as the tooltip.
	import { personName } from './people.ts';
	import type { People } from './types.ts';

	let { principal, people = {} }: { principal: string; people?: People } = $props();
</script>

<span title={principal} data-testid="person" data-principal={principal}
	>{personName(principal, people)}</span
>
