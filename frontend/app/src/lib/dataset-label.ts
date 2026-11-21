/**
 * A dataset's name as a person reads it: its id is the source's name, a `~`, and the namespace within
 * the source (`workbench.sources`), the root's being empty. Pure, so the list and the dataset's own
 * page name it alike.
 */

/** The source and the namespace within it; `null` namespace for the source's root. */
export function datasetParts(id: string): { source: string | null; namespace: string | null } {
	const at = id.indexOf('~');
	if (at === -1) return { source: null, namespace: id };
	const namespace = id.slice(at + 1);
	return { source: id.slice(0, at), namespace: namespace === '' ? null : namespace };
}

/** `source · namespace`, or the source alone for its root. */
export function datasetLabel(id: string): string {
	const { source, namespace } = datasetParts(id);
	if (source === null) return id;
	return namespace === null ? source : `${source} · ${namespace}`;
}
