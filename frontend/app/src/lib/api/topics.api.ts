/**
 * The dataset's topic hierarchy, read from the viewer's `GET /api/topics` as the person: whether it is
 * built, and the nested tree the treemap and the flow draw.
 */
import * as v from 'valibot';
import { getTopics } from '@workbench/explorer-api';
import { query } from '#lib/reads.svelte.ts';

/** `dataset` keys the read: the client reads the active view's. */
export const topicTree = query(v.string(), () => getTopics());
