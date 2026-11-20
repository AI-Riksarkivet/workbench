/**
 * `/config.json`: what the deployment tells the built app at boot, so the image carries no
 * environment. The release renders it from its values (`chart/`'s
 * `app.config`); `vite dev` serves `dev/config.json`.
 *
 * - `services` names each service's PUBLIC base: the path the Gateway routes to it and rewrites to the
 *   service's own root. It is also what a DPoP proof's `htu` names (RFC 9449 § 4.2): the browser signs
 *   `<this origin><base><path>`, and each service is configured with the same `<origin><base>` as its
 *   `WORKBENCH_PUBLIC_URL`, so the two agree with nothing in between to translate.
 * - `issuer`, `clientId` and `scopes` are the workbench's own public client at the platform's OIDC
 *   provider. The scopes are how the token comes to name its audiences: where the provider exchanges no
 *   token for the lakehouse (Dex), they ask for the lakehouse's audience too (Dex's
 *   `audience:server:client_id:<audience>`), and the services pass the person's token on. A stack that
 *   signs nobody in says `{"signIn": false}` instead; a missing or malformed config is an error the app
 *   refuses to run past, never a quiet fall-back to signing nobody in.
 * - `dpop` (optional): whether tokens are bound to the tab with DPoP (`auth.svelte.ts`). `required` (where
 *   the services require a proof) signs in only at a provider whose discovery offers DPoP, and refuses to
 *   run otherwise; `offered` (the default) binds where the provider offers it and presents a bearer where
 *   it does not; `off` never asks for a bound token.
 * - `silentSignIn` (optional, on by default): whether a sign-in first asks the issuer with `prompt=none`, so a reload
 *   is signed in again from the issuer's own session with no form (`auth.svelte.ts`). Off for an issuer that keeps no
 *   session of its own (Dex without `DEX_SESSIONS_ENABLED`), which shows its form for `prompt=none` and may then refuse
 *   it, so the person would meet the form twice.
 * - `canvas` (optional) tunes the labeling canvas: `autosaveIdleSeconds`, how long after the last edit an autosave
 *   writes (4 by default).
 */
import * as v from 'valibot';

/** A same-origin path: one leading slash, no trailing one, no query, fragment or scheme. */
const BasePath = v.pipe(
	v.string(),
	v.regex(
		/^\/[A-Za-z0-9._~/-]*[A-Za-z0-9._~-]$/,
		'a service base is a same-origin path like /api/viewer',
	),
	v.check((path) => !path.includes('//'), 'a service base has no empty segment'),
);

const ServicesSchema = v.object({ viewer: BasePath, search: BasePath, annotator: BasePath });
export type Services = v.InferOutput<typeof ServicesSchema>;
export type ServiceName = keyof Services;

/** Whether `issuer`, resolved against this origin, is one the app may sign in at: https, or http only on a loopback
 *  host for development (RFC 6749 § 3.1 requires TLS at the authorization endpoint; RFC 10017 keeps a browser client
 *  to it). */
function secureIssuer(issuer: string, origin: string): boolean {
	let url: URL;
	try {
		url = new URL(issuer, origin);
	} catch {
		return false;
	}
	if (url.protocol === 'https:') return true;
	return url.protocol === 'http:' && LOOPBACK.has(url.hostname);
}

const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);

const CanvasSchema = v.object({
	autosaveIdleSeconds: v.optional(v.pipe(v.number(), v.minValue(1), v.maxValue(86_400)), 4),
});
export type CanvasSettings = v.InferOutput<typeof CanvasSchema>;
const DEFAULT_CANVAS: CanvasSettings = { autosaveIdleSeconds: 4 };

const SignInSchema = v.object({
	/** Absolute, or a path on this origin when the Gateway serves the issuer same-origin. */
	issuer: v.pipe(v.string(), v.minLength(1)),
	clientId: v.pipe(v.string(), v.minLength(1)),
	scopes: v.array(v.string()),
	dpop: v.optional(v.picklist(['required', 'offered', 'off']), 'offered'),
	silentSignIn: v.optional(v.boolean(), true),
	services: ServicesSchema,
	canvas: v.optional(CanvasSchema, DEFAULT_CANVAS),
});
const SignInOffSchema = v.object({
	signIn: v.literal(false),
	services: ServicesSchema,
	canvas: v.optional(CanvasSchema, DEFAULT_CANVAS),
});
export type SignInConfig = v.InferOutput<typeof SignInSchema>;

export type ConfigRead =
	| { off: true; services: Services; canvas: CanvasSettings }
	| { off: false; config: SignInConfig }
	| { error: string };

/** Read and validate `/config.json`: one of the two shapes, or why it cannot be used. `origin` is the page's, which a
 *  same-origin issuer path is read against. */
export async function readConfig(
	fetch: typeof globalThis.fetch,
	origin: string = location.origin,
): Promise<ConfigRead> {
	let body: unknown;
	try {
		const res = await fetch('/config.json', { headers: { accept: 'application/json' } });
		if (!res.ok) return { error: `/config.json answered ${res.status}` };
		body = await res.json();
	} catch (err) {
		return { error: `/config.json could not be read: ${String(err)}` };
	}
	// The two shapes are told apart by `signIn` alone, so a malformed config is reported against the
	// shape it meant to be, field by field.
	const off = typeof body === 'object' && body !== null && 'signIn' in body;
	const read = off ? v.safeParse(SignInOffSchema, body) : v.safeParse(SignInSchema, body);
	if (!read.success) return { error: `/config.json is malformed: ${v.summarize(read.issues)}` };
	if ('signIn' in read.output)
		return { off: true, services: read.output.services, canvas: read.output.canvas };
	if (!secureIssuer(read.output.issuer, origin)) {
		return {
			error: `/config.json names an issuer that is not https: ${read.output.issuer} (http only on a loopback host)`,
		};
	}
	return { off: false, config: read.output };
}
