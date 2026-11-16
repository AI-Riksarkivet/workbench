import type { BrowserContext, Page } from '@playwright/test';
import type { LakehouseCall } from './mock-lakehouse';
import type { MockIdentity } from './mock-oidc';
import { EDGE } from './ports';

/** A person to sign in as, unique to the spec that asks, so the mock services' ledger of one spec's
 *  calls is that spec's alone. */
export function person(name: string): MockIdentity {
	const sub = `user:${name.toLowerCase()}-${crypto.randomUUID().slice(0, 8)}`;
	return { sub, name, email: `${name.toLowerCase()}@example.com` };
}

/** Tell the mock issuer who signs in: the app's real PKCE and DPoP round trip does the rest. */
export async function signIn(context: BrowserContext, who: MockIdentity): Promise<void> {
	await context.addCookies([
		{ name: 'mock_oidc_identity', value: encodeURIComponent(JSON.stringify(who)), url: EDGE },
	]);
}

/** Collect every CSP violation the page reports, from before its first script runs. */
export async function watchCsp(page: Page): Promise<() => Promise<string[]>> {
	await page.addInitScript(() => {
		const seen: string[] = [];
		(window as unknown as { __csp: string[] }).__csp = seen;
		document.addEventListener('securitypolicyviolation', (event) => {
			seen.push(`${event.violatedDirective} ${event.blockedURI}`);
		});
	});
	return () => page.evaluate(() => (window as unknown as { __csp: string[] }).__csp ?? []);
}

/** What the mock services recorded for one person. */
export interface Call {
	service: string;
	method: string;
	path: string;
	/** The query string the call carried, as the viewer recorded it. */
	query?: string;
	status: number;
	refused?: string;
}

export async function callsOf(page: Page, who: MockIdentity): Promise<Call[]> {
	const res = await page.request.get(
		`${EDGE}/api/viewer/__calls?sub=${encodeURIComponent(who.sub)}`,
	);
	return (await res.json()) as Call[];
}

/** What the lakehouse's door admitted for one person (`mock-lakehouse.ts`). */
export async function lakehouseOf(page: Page, who: MockIdentity): Promise<LakehouseCall[]> {
	const res = await page.request.get(
		`${EDGE}/api/viewer/__lakehouse?sub=${encodeURIComponent(who.sub)}`,
	);
	return (await res.json()) as LakehouseCall[];
}

/** The grants the issuer gave one person, in order (`mock-oidc.ts`). */
export async function grantsOf(page: Page, who: MockIdentity): Promise<string[]> {
	const res = await page.request.get(`${EDGE}/oidc/__grants?sub=${encodeURIComponent(who.sub)}`);
	return (await res.json()) as string[];
}
