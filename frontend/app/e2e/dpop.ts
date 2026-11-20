// RFC 9449 for the e2e mocks: the proof rules the workbench's services enforce
// (`service_kit.governed.dpop`), and the RFC 7638 thumbprint a token's `cnf.jkt` names. The mock
// issuer binds every token it issues to the key of the proof its token request carried, and the mock
// services refuse a call whose proof is missing, malformed, for another method or URL, stale, replayed,
// or signed by another key: a call the app signs wrongly fails here as it would live.

const decoder = new TextDecoder();
const b64urlBytes = (text: string): Uint8Array<ArrayBuffer> =>
	new Uint8Array(Buffer.from(text, 'base64url'));
const b64urlJSON = (text: string): unknown => JSON.parse(decoder.decode(b64urlBytes(text)));

/** The public P-256 key a proof's header carries. */
interface EcJwk {
	kty: 'EC';
	crv: 'P-256';
	x: string;
	y: string;
}

function isEcJwk(value: unknown): value is EcJwk {
	if (typeof value !== 'object' || value === null) return false;
	const jwk = value as Record<string, unknown>;
	return (
		jwk.kty === 'EC' &&
		jwk.crv === 'P-256' &&
		typeof jwk.x === 'string' &&
		typeof jwk.y === 'string' &&
		!('d' in jwk)
	);
}

/** RFC 7638: base64url SHA-256 of the required members, in lexical order, no whitespace. */
function thumbprint(jwk: EcJwk): string {
	const canonical = JSON.stringify({ crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y });
	return new Bun.CryptoHasher('sha256').update(canonical).digest('base64url');
}

/** base64url SHA-256 of an access token: a proof's `ath`. */
export const athOf = (token: string): string =>
	new Bun.CryptoHasher('sha256').update(token).digest('base64url');

/** What a verified proof says. */
export interface Proof {
	jkt: string;
	htm: string;
	htu: string;
	jti: string;
	ath: string | undefined;
}

/** RFC 3986 § 2.3's unreserved characters, whose percent-encoding is the character itself. */
const UNRESERVED = /^[A-Za-z0-9\-._~]$/;

/** `url` as RFC 9449 § 4.3 rule 9 compares it, the way the services do (`service_kit.governed.dpop`): no query or
 *  fragment, the scheme and host lower-cased and the default port dropped (the WHATWG parser does both), and the path's
 *  percent-encoding normalised by RFC 3986 § 6.2.2: hex digits upper-cased, unreserved characters decoded, a reserved
 *  character left as it was sent. */
function normalisedHtu(url: string): string {
	const parsed = new URL(url);
	const path = parsed.pathname.replace(/%([0-9A-Fa-f]{2})/g, (_, hex: string) => {
		const char = String.fromCharCode(parseInt(hex, 16));
		return UNRESERVED.test(char) ? char : `%${hex.toUpperCase()}`;
	});
	return `${parsed.protocol}//${parsed.host}${path || '/'}`;
}

const seen = new Set<string>();
/** How far a proof's `iat` may lie from now, as the services' default window. */
const WINDOW_SECONDS = 60;

/** Verify one proof for `method` and `htu` (the URL the request came to, as it came over the wire), or throw saying
 *  which rule it breaks. */
export async function verifyProof(
	proof: string | null,
	method: string,
	htu: string,
): Promise<Proof> {
	if (!proof) throw new Error('no DPoP proof');
	const parts = proof.split('.');
	if (parts.length !== 3) throw new Error('the proof is not a compact JWS');
	const [rawHeader = '', rawPayload = '', rawSignature = ''] = parts;
	const header = b64urlJSON(rawHeader) as Record<string, unknown>;
	if (header.typ !== 'dpop+jwt') throw new Error('typ is not dpop+jwt');
	if (header.alg !== 'ES256') throw new Error('alg is not ES256');
	if (!isEcJwk(header.jwk)) throw new Error('jwk is not a public P-256 key');
	const key = await crypto.subtle.importKey(
		'jwk',
		header.jwk,
		{ name: 'ECDSA', namedCurve: 'P-256' },
		false,
		['verify'],
	);
	const valid = await crypto.subtle.verify(
		{ name: 'ECDSA', hash: 'SHA-256' },
		key,
		b64urlBytes(rawSignature),
		new TextEncoder().encode(`${rawHeader}.${rawPayload}`),
	);
	if (!valid) throw new Error('the signature does not verify with its jwk');
	const claims = b64urlJSON(rawPayload) as Record<string, unknown>;
	if (claims.htm !== method) throw new Error(`htm ${String(claims.htm)} is not ${method}`);
	if (typeof claims.htu !== 'string' || normalisedHtu(claims.htu) !== normalisedHtu(htu)) {
		throw new Error(`htu ${String(claims.htu)} is not ${htu}`);
	}
	if (typeof claims.iat !== 'number' || Math.abs(Date.now() / 1000 - claims.iat) > WINDOW_SECONDS) {
		throw new Error('iat is outside the window');
	}
	if (typeof claims.jti !== 'string' || claims.jti === '') throw new Error('no jti');
	if (seen.has(claims.jti)) throw new Error('jti was already used');
	seen.add(claims.jti);
	return {
		jkt: thumbprint(header.jwk),
		htm: claims.htm,
		htu: claims.htu,
		jti: claims.jti,
		ath: typeof claims.ath === 'string' ? claims.ath : undefined,
	};
}

/** The key an issuer that never restarted signs with. */
export const FIRST_KEY = 'k1';

/** What a mock access token carries. Unsigned: only the mocks read it, and what they check is its issuer, its
 *  audiences and, for a bound token, the binding to the proof's key, which a token cannot fake. */
export interface TokenClaims {
	iss: string;
	aud: string[];
	sub: string;
	name: string;
	email: string;
	/** The key the token is bound to; absent for a provider that binds none. */
	cnf?: { jkt: string };
	/** The client the token was issued to, where it names audiences beside it (Dex's cross-client tokens). */
	azp?: string;
	/** Dex's claims for the `federated:id` scope: the connector and the user id it reports. */
	federated_claims?: { connector_id: string; user_id: string };
	/** The issuer's signing key that a real token's header names; absent, the issuer's first key (`mock-oidc.ts`). */
	kid?: string;
	exp: number;
}

export const mintToken = (claims: TokenClaims): string =>
	`dpop-e2e.${Buffer.from(JSON.stringify(claims)).toString('base64url')}`;

export function readToken(token: string): TokenClaims | null {
	if (!token.startsWith('dpop-e2e.')) return null;
	try {
		return b64urlJSON(token.slice('dpop-e2e.'.length)) as TokenClaims;
	} catch {
		return null;
	}
}
