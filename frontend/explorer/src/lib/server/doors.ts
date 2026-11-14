/**
 * The upstream door the `/workflow` remainder calls through until #9's D4, over `@workbench/api/upstream`:
 * the annotator's media plane, with the signed-in session's bearer.
 *
 * `$lib/server/` on purpose: SvelteKit's illegal-import check makes the server-only boundary structural.
 */

import { getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import type { ApiResult } from '@workbench/api/client';
import { upstreamJSON } from '@workbench/api/upstream';

const ANNOTATOR_API = env.ANNOTATOR_API ?? 'http://localhost:8103';

function doorTo(
	base: string,
	upstream: string,
	path: string,
	init?: RequestInit,
): Promise<ApiResult<unknown>> {
	const { fetch, locals } = getRequestEvent();
	return upstreamJSON({
		fetch,
		base,
		path,
		init,
		bearer: locals.session?.accessToken,
		upstream,
	});
}

/** One media-plane call into the annotator → `ApiResult<unknown>`; the session's bearer rides along. */
export function annotatorJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return doorTo(ANNOTATOR_API, 'annotator', path, init);
}

/** The write gate the deleted POST routes carried (`requireSession: true`): on an auth-enabled
 *  stack a write must be attributable — it seeds task provenance — so an anonymous caller is
 *  refused before the request leaves the zone. The reads had no such gate and still have none. */
export function sessionGate(): { ok: false; status: number; detail: string } | null {
	const { locals } = getRequestEvent();
	if (locals.authEnabled && !locals.session) {
		return { ok: false, status: 401, detail: 'sign in required' };
	}
	return null;
}
