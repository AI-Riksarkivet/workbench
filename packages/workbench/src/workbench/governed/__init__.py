"""Sign-in, authorization and the audit trail every service shares: the OIDC verifier and DPoP (``oidc``, ``dpop``),
the request's caller (``deps``, ``identity``, ``principal``), the workbench's own OpenFGA (``fga``, ``auth``), token
exchange to a lakehouse (``token_exchange``) and the audit trail (``audit``)."""
