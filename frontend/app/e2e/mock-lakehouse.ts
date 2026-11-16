// The lakehouse, as the mock services reach it, the way the workbench's services do (`service_kit.governed.deps`,
// `token_exchange`): a person's read on a token addressed to the lakehouse, exchanged at a Keycloak-shaped provider and
// the person's own token passed on at a Dex-shaped one; the workbench's own write on its client-credentials token,
// asking Dex for the lakehouse's audience by its scope. The door admits only a token naming the lakehouse's audience
// and bound to no key, as the lakehouse's doors do, and records whom it admitted on what: `GET
// /api/viewer/__lakehouse?sub=` answers one person's calls.

import { readToken, type TokenClaims } from './dpop';
import { MOCK_OIDC_PORT } from './ports';
import {
	LAKEHOUSE_AUDIENCE,
	SERVICE_CLIENT,
	SERVICE_SECRET,
	SHAPES,
	audienceScope,
	shapeOf,
} from './providers';

/** One call the lakehouse's door admitted or refused. */
export interface LakehouseCall {
	/** Whom the call was made as: the person, or the workbench's own client. */
	as: 'person' | 'service';
	/** How the token came to name the lakehouse: exchanged for the person, the person's own passed on, or the
	 *  service's own. */
	token: 'exchanged' | 'passed-on' | 'own';
	/** The token's subject, as the door read it. */
	principal: string;
	admitted: boolean;
}

const ledger = new Map<string, LakehouseCall[]>();

export const lakehouseCallsOf = (sub: string): LakehouseCall[] => ledger.get(sub) ?? [];

/** The door: a token addressed to the lakehouse and bound to no key, or a refusal. */
function door(
	person: string,
	call: Omit<LakehouseCall, 'principal' | 'admitted'>,
	token: string,
): boolean {
	const claims = readToken(token);
	const admitted = !!claims && claims.aud.includes(LAKEHOUSE_AUDIENCE) && !claims.cnf;
	ledger.set(person, [
		...lakehouseCallsOf(person),
		{ ...call, principal: claims?.sub ?? '', admitted },
	]);
	return admitted;
}

/** Ask the issuer `iss` names, as the workbench's confidential client, for `form`'s grant. */
async function ask(
	iss: string,
	host: string,
	form: Record<string, string>,
): Promise<string | null> {
	const res = await fetch(`http://localhost:${MOCK_OIDC_PORT}/token`, {
		method: 'POST',
		headers: {
			authorization: `Basic ${Buffer.from(`${SERVICE_CLIENT}:${SERVICE_SECRET}`).toString('base64')}`,
			'content-type': 'application/x-www-form-urlencoded',
			'x-forwarded-host': host,
			'x-forwarded-prefix': SHAPES[shapeOf(iss)].prefix,
		},
		body: new URLSearchParams(form),
	});
	if (!res.ok) return null;
	return ((await res.json()) as { access_token: string }).access_token;
}

/** A read of the lakehouse as the person `claims` names, on their token `token`: whether the door admitted it. */
export async function readAsPerson(
	claims: TokenClaims,
	token: string,
	host: string,
): Promise<boolean> {
	if (shapeOf(claims.iss) === 'dex')
		return door(claims.sub, { as: 'person', token: 'passed-on' }, token);
	const exchanged = await ask(claims.iss, host, {
		grant_type: 'urn:ietf:params:oauth:grant-type:token-exchange',
		subject_token: token,
		subject_token_type: 'urn:ietf:params:oauth:token-type:access_token',
		requested_token_type: 'urn:ietf:params:oauth:token-type:access_token',
		audience: LAKEHOUSE_AUDIENCE,
	});
	return exchanged !== null && door(claims.sub, { as: 'person', token: 'exchanged' }, exchanged);
}

/** A write to the lakehouse as the workbench's own client, for the person `claims` names: whether the door admitted it. */
export async function writeAsService(claims: TokenClaims, host: string): Promise<boolean> {
	const own = await ask(claims.iss, host, {
		grant_type: 'client_credentials',
		audience: LAKEHOUSE_AUDIENCE,
		...(shapeOf(claims.iss) === 'dex'
			? { scope: `openid ${audienceScope(LAKEHOUSE_AUDIENCE)}` }
			: {}),
	});
	return own !== null && door(claims.sub, { as: 'service', token: 'own' }, own);
}
