# 0020. A reload keeps the session through the issuer's own session, with no token stored (2026-11-20)

**Status:** accepted. Source: the owner, 2026-11-20, in the grilling session, on staying signed in across a reload:
"ofc fix it. best practices".

## Context

The app keeps its tokens in memory, so a reload shows the issuer's sign-in page again. Surviving a reload means either
keeping a token or a refresh token where a script in the page can read it, or asking the issuer, which holds the
person's login session, to sign them in again without a form.

## Decision

- Tokens stay in memory. Nothing that can be presented as the person is written to browser storage.
- After a reload the app asks the issuer for a new sign-in without prompting, and shows the sign-in page only when the
  issuer no longer holds a session for the person.

## Consequences

- An issuer that offers no session of its own (or a browser that withholds its cookies from that request) still shows
  the sign-in page on reload; the app must stay correct in that case.
- Signing out must end the issuer's session too, or the next reload signs the person back in.
