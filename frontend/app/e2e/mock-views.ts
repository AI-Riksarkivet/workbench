// The annotator's shared views (ADR 0022), as `annotator.api.v1.endpoints.views` answers them: one store for every
// person, since a view one person shares is opened by another by its link. Opening, and sharing, is a read of the view's
// dataset under the caller's own rights: the person who signs in as `Outsider` may read no source, and is refused as
// that read is refused (403, `<sub> may not read source '<source>'`); everyone else may read every dataset. A view
// nobody shares, or whose owner stopped sharing it, is a 404 whoever asks; the listing answers the caller's own views of
// one dataset, newest first, at most 200 with `more` saying whether it shares more; stopping is its owner's alone, a 404 for anyone else.

/** A shared view as the annotator keeps it (`annotator.state.store.SharedView`). */
interface SharedView {
	view_id: string;
	owner: string;
	dataset: string;
	name: string;
	snapshot: Record<string, unknown>;
	shared_at: string;
}

/** `MAX_SNAPSHOT_BYTES`: the most a view's snapshot may hold, serialized. */
const MAX_SNAPSHOT_BYTES = 64 * 1024;
/** `MAX_LISTED`: the most views one listing answers. */
const MAX_LISTED = 200;
const VIEW_ID = /^[A-Za-z0-9_-]{1,64}$/;

const views = new Map<string, SharedView>();

const json = (body: unknown, status = 200): Response =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** An RFC 9457 problem with its `detail`. */
const problem = (status: number, detail: string): Response =>
	new Response(JSON.stringify({ type: 'about:blank', status, detail }), {
		status,
		headers: { 'content-type': 'application/problem+json' },
	});

/** FastAPI's refusal of a request that does not validate. */
const invalid = (msg: string): Response => json({ detail: [{ msg }] }, 422);

/** Python's `repr` of a string, as the doors quote an id. */
const repr = (value: string): string => `'${value}'`;

/** The caller's read of `dataset`: null when it may read it, else the refusal. */
function refusedRead(sub: string, name: string | undefined, dataset: string): Response | null {
	if (name !== 'Outsider') return null;
	const source = dataset.split('~')[0] ?? dataset;
	return problem(403, `${sub} may not read source ${repr(source)}`);
}

/** Answer one call to the `/views` doors, or null when the path is not one of them. */
export async function viewsDoor(
	req: Request,
	url: URL,
	{ sub, name, record }: { sub: string; name?: string; record: (status: number) => void },
): Promise<Response | null> {
	const path = url.pathname;
	if (path !== '/views' && !path.startsWith('/views/')) return null;
	const answer = (res: Response): Response => {
		record(res.status);
		return res;
	};

	if (path === '/views' && req.method === 'POST') {
		const body = (await req.json().catch(() => null)) as Record<string, unknown> | null;
		const dataset = body?.dataset;
		const title = body?.name;
		const snapshot = body?.snapshot;
		if (typeof dataset !== 'string' || dataset.length < 1 || dataset.length > 512)
			return answer(invalid('dataset'));
		if (typeof title !== 'string' || title.trim().length < 1 || title.length > 200)
			return answer(invalid('name'));
		if (typeof snapshot !== 'object' || snapshot === null || Array.isArray(snapshot))
			return answer(invalid('snapshot'));
		const size = new TextEncoder().encode(JSON.stringify(snapshot)).length;
		if (size > MAX_SNAPSHOT_BYTES)
			return answer(
				invalid(
					`a view's snapshot holds at most ${MAX_SNAPSHOT_BYTES} bytes; this one holds ${size}`,
				),
			);
		const refused = refusedRead(sub, name, dataset);
		if (refused) return answer(refused);
		const view: SharedView = {
			view_id: crypto.randomUUID().replaceAll('-', ''),
			owner: sub,
			dataset,
			name: title,
			snapshot: snapshot as Record<string, unknown>,
			shared_at: new Date().toISOString(),
		};
		views.set(view.view_id, view);
		return answer(json(view, 201));
	}
	if (path === '/views' && req.method === 'GET') {
		const dataset = url.searchParams.get('dataset');
		if (!dataset) return answer(invalid('dataset'));
		const mine = [...views.values()]
			.filter((v) => v.owner === sub && v.dataset === dataset)
			.reverse();
		return answer(json({ views: mine.slice(0, MAX_LISTED), more: mine.length > MAX_LISTED }));
	}
	const id = decodeURIComponent(path.slice('/views/'.length));
	if (!VIEW_ID.test(id)) return answer(invalid('view_id'));
	if (req.method === 'GET') {
		const view = views.get(id);
		if (!view)
			return answer(
				problem(404, `nobody shares a view ${repr(id)}: its owner may have stopped sharing it`),
			);
		return answer(refusedRead(sub, name, view.dataset) ?? json(view));
	}
	if (req.method === 'DELETE') {
		const view = views.get(id);
		if (!view || view.owner !== sub) return answer(problem(404, `you share no view ${repr(id)}`));
		views.delete(id);
		record(204);
		return new Response(null, { status: 204 });
	}
	return null;
}
