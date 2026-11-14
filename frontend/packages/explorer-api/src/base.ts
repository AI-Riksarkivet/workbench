/**
 * Where `/api/*` lives and how it is reached: the package's one transport seam.
 *
 * Two consumers install it differently, and the package names neither:
 *
 *  - The browser-only app installs a TRANSPORT (`setTransport`): its own fetch, which signs every call
 *    with the tab's DPoP key and times it out, and a URL map that puts each service's path on that
 *    service's public base (the Gateway routes the base to the service). Nothing in this package holds
 *    a token or knows a base.
 *  - The older apps, which still serve `/api/*` through their own proxy routes, set a BASE (`setApiBase`)
 *    and keep the platform `fetch`.
 *
 * Framework-agnostic on purpose: this package never imports `$app/*`.
 */

/** The fetch this package calls through: the platform's, or the one a transport installs. */
export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

/** How the browser-only app reaches the services: its signed fetch, and where each path lives. */
export interface Transport {
	fetch: Fetcher;
	/** The URL of a service path (`/api/search?...`, `/api/documents?...`), on that service's base. */
	url: (path: string) => string;
}

let apiBase = '';
let transport: Transport | null = null;

/** Set the base path prefixed to every `/api/*` URL this package (and @workbench/labeling)
 *  builds — the zone's SvelteKit `base`. Idempotent; trailing slash is normalized.
 *
 *  This is module-level mutable state, which is only safe because a zone is a PROCESS: media and
 *  annotator are separate images, separate pods and separate dev servers, so a server process only
 *  ever sees one base. That invariant is invisible at the call site and would fail as wrong URLs on
 *  a shared SSR request path rather than as an error, so it is asserted instead of assumed —
 *  re-basing to a DIFFERENT non-empty value throws. */
export function setApiBase(b: string): void {
	const next = b.replace(/\/$/, '');
	if (apiBase && next && next !== apiBase) {
		throw new Error(
			`@workbench/explorer-api: base already set to "${apiBase}", refusing to re-base to "${next}". ` +
				`Two zones share this module instance — the media plane's URLs cannot be per-request state.`,
		);
	}
	if (next && transport) {
		throw new Error(
			'@workbench/explorer-api: a transport is installed, so a base would be ignored',
		);
	}
	// An empty base is the default, so setting it is never meaningful — and letting it through would
	// let an unbased consumer silently un-base a zone that had already configured itself.
	if (next) apiBase = next;
}

/** Install the app's transport, once per page life: a second, different one is refused, since every
 *  read already in flight was addressed through the first. */
export function setTransport(next: Transport): void {
	if (apiBase) {
		throw new Error(`@workbench/explorer-api: base "${apiBase}" is set, so a transport is refused`);
	}
	if (transport && transport !== next) {
		throw new Error('@workbench/explorer-api: a transport is already installed');
	}
	transport = next;
}

/** Test-only: drop the configured base and transport. Production code must never call this. */
export function resetApiBase(): void {
	apiBase = '';
	transport = null;
}

/** The fetch every call goes through: the installed transport's, else the platform's. A stable
 *  function, so a caller can tell the default apart from a fetcher a test injects. */
export const apiFetch: Fetcher = (input, init) => (transport?.fetch ?? fetch)(input, init);

/** A service path's URL: on its service's base under a transport, else under the zone base
 *  (`apiUrl('/api/health')` → `/explorer/api/health` once the zone called `setApiBase('/explorer')`). */
export function apiUrl(path: string): string {
	return transport ? transport.url(path) : `${apiBase}${path}`;
}
