/**
 * The dock stores speak `fetch(endpoint)` / `fetch(endpoint, {method:'PUT'})`; this adapter answers those two calls
 * from the person's browser store and with a real `Response`, so the stores' three outcomes (ok / absent / unreadable)
 * survive unchanged. A store nobody can use (status 0) answers 503: a `Response` cannot carry status 0.
 */
import { readUserStateDoc, writeUserStateDoc } from '$lib/browser-user-state';
import type { UserStateDocument } from '$lib/user-state';

const asJSON = (body: unknown, status = 200): Response =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

export function userStateFetcher(document: UserStateDocument): typeof fetch {
	// Bun's `typeof fetch` carries `preconnect`; the stores only ever call the function itself, so the
	// shim declares the call signature and is cast at the boundary rather than faking a static method.
	const call = async (_input: URL | RequestInfo, init?: RequestInit): Promise<Response> => {
		if (init?.method === 'PUT') {
			const value: unknown = JSON.parse(String(init.body ?? 'null'));
			const res = await writeUserStateDoc({ document, value });
			return res.ok
				? new Response(null, { status: 204 })
				: new Response(null, { status: res.status || 503 });
		}
		const res = await readUserStateDoc({ document });
		return res.ok ? asJSON(res.data) : new Response(null, { status: res.status || 503 });
	};
	return call as unknown as typeof fetch;
}
