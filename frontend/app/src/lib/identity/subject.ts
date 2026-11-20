// How an authorization subject is shown. A subject carries the OIDC `sub` verbatim, which for some issuers (Dex) is
// an opaque base64 blob ("CiQwOGE4…bG9jYWw"): two of them side by side read as one run-on string, and neither is
// recognisable as a person. There is no directory to resolve a sub against, so the fix is typographic: readable
// identifiers stay verbatim, opaque ones are shortened in the middle, and the FULL subject is always the `title`, so
// hover and copy still yield the value a grant needs.

/** A subject is "readable" when a person can recognise it: emails, `role:…#assignee`, short names like `root_admin`.
 *  Opaque = one long unbroken token (an OIDC sub, a UUID-like id). */
const OPAQUE_RE = /^[A-Za-z0-9_-]{25,}$/;

export type SubjectDisplay = {
	/** What to render. */
	label: string;
	/** The full subject, always set, so every rendering can carry it as a tooltip. */
	title: string;
};

/** Shorten the middle of one opaque token: `CiQwOGE4Njg0…bG9jYWw`. Callers gate on OPAQUE_RE first, which already
 *  requires at least 25 characters, so no second length threshold is needed here. */
function shorten(token: string): string {
	return `${token.slice(0, 12)}…${token.slice(-7)}`;
}

/** Display form of one subject. `user:<opaque-sub>` keeps its type prefix; a bare opaque token is shortened alone;
 *  anything readable passes through untouched. */
export function subjectDisplay(subject: string): SubjectDisplay {
	const sep = subject.indexOf(':');
	if (sep > 0) {
		const type = subject.slice(0, sep);
		const rest = subject.slice(sep + 1);
		if (OPAQUE_RE.test(rest)) return { label: `${type}:${shorten(rest)}`, title: subject };
		return { label: subject, title: subject };
	}
	if (OPAQUE_RE.test(subject)) return { label: shorten(subject), title: subject };
	return { label: subject, title: subject };
}
