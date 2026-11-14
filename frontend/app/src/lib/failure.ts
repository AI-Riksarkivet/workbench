import { isHttpError } from '@sveltejs/kit';
import { ApiError } from '@workbench/explorer-api';

/** What a failed read says, in the service's own words: a read through `reads.svelte.ts`
 *  (`orFail`) throws SvelteKit's HTTP error, a call through the explorer's client its `ApiError`. */
export function failureText(err: unknown): string {
	if (isHttpError(err)) return err.body.message;
	if (err instanceof ApiError) return err.detail;
	return err instanceof Error ? err.message : String(err);
}
