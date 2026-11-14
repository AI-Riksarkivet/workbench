// The e2e stand-in for the platform's Gateway: one origin that routes each service's public base to
// its (mock) service, rewriting the base to the service's root as a deployment's HTTPRoutes do, and
// everything else to the app's own static server, `serve.ts`, the very server the image ships, so its
// CSP and SPA fallback are what the specs meet. Bun-native on purpose: Bun's `node:http` emulation is
// not reliable under a Node HTTP proxy.
//
// Each upstream is told the browser's host (`x-forwarded-host`) and the base it was reached under
// (`x-forwarded-prefix`): the public URL a DPoP proof's `htu` names, which a real service is configured
// with (`WORKBENCH_PUBLIC_URL`).
//
//   PORT                       the origin the browser uses
//   VIEWER_API, SEARCH_API,    /api/viewer, /api/search, /api/annotator → each service's root
//   ANNOTATOR_API
//   OIDC_MOCK                  /oidc → the mock issuer
//   WORKBENCH_APP_CONFIG_FILE  passed through to serve.ts (absent: /config.json 404s and the app refuses to run)
//   INNER is PORT + 1000, where serve.ts listens behind this edge.

const PORT = Number(process.env.PORT);
const INNER = PORT + 1000;

const env = process.env;
// Each route strips its prefix: the upstream answers at its root.
const routes: { prefix: string; target: string | undefined }[] = [
	{ prefix: '/api/viewer', target: env.VIEWER_API },
	{ prefix: '/api/search', target: env.SEARCH_API },
	{ prefix: '/api/annotator', target: env.ANNOTATOR_API },
	{ prefix: '/oidc', target: env.OIDC_MOCK },
];

const inner = Bun.spawn(['bun', 'serve.ts'], {
	cwd: new URL('..', import.meta.url).pathname,
	env: { ...process.env, PORT: String(INNER) },
	stdout: 'inherit',
	stderr: 'inherit',
});
// The edge owns its serve.ts: stopping the edge stops it, or a stopped suite leaves the app
// listening on INNER, and the next run's edge fails to bind there.
process.on('exit', () => inner.kill());
for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP'] as const) {
	process.on(signal, () => {
		inner.kill();
		process.exit(0);
	});
}
for (let tries = 0; ; tries++) {
	const up = await fetch(`http://127.0.0.1:${INNER}/`).then(
		(r) => r.ok,
		() => false,
	);
	if (up) break;
	if (tries > 100) throw new Error('serve.ts did not start');
	await Bun.sleep(50);
}

/** Response headers that describe bytes `fetch` has already decoded, or the hop itself. */
const DROPPED = ['content-encoding', 'content-length', 'transfer-encoding', 'connection'];

Bun.serve({
	port: PORT,
	async fetch(req) {
		const url = new URL(req.url);
		const match = routes.find(
			(r) => r.target && (url.pathname === r.prefix || url.pathname.startsWith(`${r.prefix}/`)),
		);
		const path = match ? url.pathname.slice(match.prefix.length) || '/' : url.pathname;
		const base = match ? match.target! : `http://127.0.0.1:${INNER}`;
		const headers = new Headers(req.headers);
		headers.set('x-forwarded-host', url.host);
		if (match) headers.set('x-forwarded-prefix', match.prefix);
		headers.delete('host');
		let upstream: Response;
		try {
			upstream = await fetch(`${base}${path}${url.search}`, {
				method: req.method,
				headers,
				body: req.method === 'GET' || req.method === 'HEAD' ? undefined : await req.arrayBuffer(),
				redirect: 'manual',
			});
		} catch (err) {
			return Response.json({ detail: `upstream unreachable: ${String(err)}` }, { status: 502 });
		}
		const out = new Headers(upstream.headers);
		for (const name of DROPPED) out.delete(name);
		return new Response(upstream.body, { status: upstream.status, headers: out });
	},
});

console.log(`e2e edge on :${PORT} (app on :${INNER})`);
