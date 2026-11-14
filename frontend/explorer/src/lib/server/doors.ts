/**
 * The upstream doors the explorer's remaining pages call through (atlas and graph until #9's D2b-2,
 * `/workflow` until D4), over `@workbench/api/upstream`: the annotator's projects plane, its media plane, and the viewer's graph
 * console, each with the signed-in session's bearer. `ANNOTATOR_PROJECTS_API` falls back to
 * `ANNOTATOR_API`, one service in any real deploy.
 *
 * `$lib/server/` on purpose: SvelteKit's illegal-import check makes the server-only boundary structural.
 */

import { getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import type { ApiResult } from '@workbench/api/client';
import { upstreamJSON } from '@workbench/api/upstream';

const VIEWER_API = env.VIEWER_API ?? 'http://localhost:8101';
const PROJECTS_API = env.ANNOTATOR_PROJECTS_API ?? env.ANNOTATOR_API ?? 'http://localhost:8103';
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

/** One projects-plane call → `ApiResult<unknown>`; the signed-in session's bearer rides along. */
export function projectsJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return doorTo(PROJECTS_API, 'annotator', path, init);
}

/** One media-plane call into the annotator — same contract, the other base. */
export function annotatorJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return doorTo(ANNOTATOR_API, 'annotator', path, init);
}

/** One lance-viewer call (the graph console) — same contract, the viewer base. */
export function viewerJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return doorTo(VIEWER_API, 'viewer', path, init);
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
