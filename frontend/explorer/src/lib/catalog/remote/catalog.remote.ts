import { getRequestEvent, query } from '$app/server';
import { fetchMe, type Me } from '@workbench/api';
import { CATALOG_API } from '$lib/server/doors';

/**
 * The navbar's frozen `/management/v1/me` identity.
 *
 * Same degrade posture as before — `null` on ANY failure (signed out, 401, outage, timeout, contract
 * drift) — because identity is chrome: the navbar renders the base entry set and stays fail-closed on
 * the admin surfaces rather than 500ing the zone. The whole body is `@workbench/api`'s shared `fetchMe`,
 * exactly as `feeds.remote.ts` is `@workbench/api/runs-feed`: what cannot be shared is this file, because a
 * remote function must be declared inside an app to get its own endpoint.
 */
export const fetchMeViaBff = query(async (): Promise<Me | null> => {
	const { locals, fetch } = getRequestEvent();
	return fetchMe({
		catalogUrl: CATALOG_API,
		accessToken: locals.session?.accessToken,
		fetchFn: fetch,
	});
});
