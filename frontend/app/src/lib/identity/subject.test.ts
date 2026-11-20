// The subject shortener the members panel shows a not-yet-named principal with: two opaque Dex subjects side by side
// must not read as one run-on string, and the full value must always be the tooltip.
import { describe, expect, it } from 'vitest';
import { subjectDisplay } from './subject.ts';

const DEX_SUB = 'CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs';

describe('subjectDisplay', () => {
	it('shortens an opaque OIDC sub in the middle and keeps the full value as title', () => {
		const d = subjectDisplay(DEX_SUB);
		expect(d.label).not.toBe(DEX_SUB);
		expect(d.label).toContain('…');
		expect(d.label.length).toBeLessThan(25);
		expect(d.title).toBe(DEX_SUB);
	});

	it('keeps the type prefix on a typed opaque subject', () => {
		const d = subjectDisplay(`user:${DEX_SUB}`);
		expect(d.label.startsWith('user:')).toBe(true);
		expect(d.label).toContain('…');
		expect(d.title).toBe(`user:${DEX_SUB}`);
	});

	it.each([
		'root_admin',
		'alice@example.com',
		'role:steward#member',
		'role:reviewer#assignee',
		'*',
	])('passes the readable subject %s through verbatim', (s) => {
		expect(subjectDisplay(s)).toEqual({ label: s, title: s });
	});

	it('does not shorten a short token that merely looks base64-like', () => {
		expect(subjectDisplay('abc123').label).toBe('abc123');
	});
});
