// The e2e suite's ports, one place. The edge is the origin the browser uses; serve.ts listens behind
// it on EDGE_PORT + 1000 (`../edge.ts`).
export const EDGE_PORT = 4310;
export const EDGE = `http://localhost:${EDGE_PORT}`;
export const MOCK_OIDC_PORT = 4321;
export const MOCK_VIEWER_PORT = 4322;
export const MOCK_SEARCH_PORT = 4323;
export const MOCK_ANNOTATOR_PORT = 4324;
