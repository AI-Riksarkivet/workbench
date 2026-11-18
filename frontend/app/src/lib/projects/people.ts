/**
 * What a person is called on a page: the name they last wrote under in the annotator (the `people` beside the
 * principals an answer names, from their own verified token, `annotator.people`), else a short form of their
 * principal. The principal itself is always the tooltip, so two people with one name stay tellable apart.
 */
import type { People } from './types.ts';

/** The principal's subject, after its idp (`dex~ada` reads `ada`), cut when it is an opaque id. */
export function shortPrincipal(principal: string): string {
	const subject = principal.split('~').at(-1) ?? principal;
	return subject.length > 16 ? `${subject.slice(0, 8)}…` : subject;
}

/** The name a page shows for ``principal``. */
export function personName(principal: string | null | undefined, people: People = {}): string {
	if (!principal) return 'someone';
	return people[principal] ?? shortPrincipal(principal);
}
