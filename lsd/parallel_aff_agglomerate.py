from .labels import relabel
from .merge_tree import MergeTree
import logging
import numpy as np
import peach
import waterz

logger = logging.getLogger(__name__)

def parallel_aff_agglomerate(
        affs,
        fragments,
        rag_provider,
        block_size,
        context,
        merge_function,
        threshold,
        num_workers):
    '''Extract fragments from affinities using watershed.

    Args:

        affs (`class:peach.Array`):

            An array containing affinities.

        fragments (`class:peach.Array`):

            An array containing fragments.

        rag_provider (`class:SharedRagProvider`):

            A RAG provider to read nodes from and write found edges to.

        block_size (``tuple`` of ``int``):

            The size of the blocks to process in parallel, in world units.

        context (``tuple`` of ``int``):

            The context to consider for agglomeration, in world units.

        merge_function (``string``):

            The merge function to use for ``waterz``.

        threshold (``float``):

            Until which threshold to agglomerate.

        num_workers (``int``):

            The number of parallel workers.

    Returns:

        True, if all tasks succeeded.
    '''

    assert fragments.data.dtype == np.uint64

    shape = affs.shape[1:]
    context = peach.Coordinate(context)

    total_roi = affs.roi.grow(context, context)
    read_roi = peach.Roi((0,)*affs.roi.dims(), block_size).grow(context, context)
    write_roi = peach.Roi((0,)*affs.roi.dims(), block_size)

    return peach.run_blockwise(
        total_roi,
        read_roi,
        write_roi,
        lambda b: agglomerate_in_block(
            affs,
            fragments,
            rag_provider,
            b,
            merge_function,
            threshold),
        lambda b: block_done(b, rag_provider),
        num_workers=num_workers,
        read_write_conflict=False,
        fit='shrink')

def block_done(block, rag_provider):

    rag = rag_provider[block.write_roi]
    return rag.number_of_edges() > 0 or rag.number_of_nodes() <= 1

def agglomerate_in_block(
        affs,
        fragments,
        rag_provider,
        block,
        merge_function,
        threshold):

    logger.info(
        "Agglomerating in block %s with context of %s",
        block.write_roi, block.read_roi)

    # get the sub-{affs, fragments, graph} to work on
    affs = affs.intersect(block.read_roi)
    fragments = fragments.fill(affs.roi)
    rag = rag_provider[affs.roi]

    # waterz uses memory proportional to the max label in fragments, therefore
    # we relabel them here and use those
    fragments_relabelled, n, fragment_relabel_map = relabel(
        fragments.data,
        return_backwards_map=True)

    logger.debug("affs shape: %s", affs.shape)
    logger.debug("fragments shape: %s", fragments.shape)
    logger.debug("fragments num: %d", n)

    # So far, 'rag' does not contain any edges belonging to write_roi (there
    # might be a few edges from neighboring blocks, though). Run waterz until
    # threshold 0 to get the waterz RAG, which tells us which nodes are
    # neighboring. Use this to populate 'rag' with edges. Then run waterz for
    # the given threshold.

    # for efficiency, we create one waterz call with both thresholds
    generator = waterz.agglomerate(
            affs=affs.data,
            thresholds=[0, threshold],
            fragments=fragments_relabelled,
            scoring_function=merge_function,
            discretize_queue=256,
            return_merge_history=True,
            return_region_graph=True)

    # add edges to RAG
    _, _, initial_rag = next(generator)
    for edge in initial_rag:
        u, v = fragment_relabel_map[edge['u']], fragment_relabel_map[edge['v']]
        # this might overwrite already existing edges from neighboring blocks,
        # but that's fine, we only write attributes for edges within write_roi
        rag.add_edge(u, v, {'merge_score': None, 'agglomerated': True})

    # agglomerate fragments using affs
    _, merge_history, _ = next(generator)

    # create a merge tree from the merge history
    merge_tree = MergeTree(fragment_relabel_map)
    for merge in merge_history:

        a, b, c, score = merge['a'], merge['b'], merge['c'], merge['score']
        merge_tree.merge(
            fragment_relabel_map[a],
            fragment_relabel_map[b],
            fragment_relabel_map[c],
            score)

    # mark edges in original RAG with score at time of merging
    logger.debug("marking merged edges...")
    num_merged = 0
    for u, v, data in rag.edges(data=True):
        merge_score = merge_tree.find_merge(u, v)
        data['merge_score'] = merge_score
        if merge_score is not None:
            num_merged += 1

    logger.info("merged %d edges", num_merged)

    # write back results (only within write_roi)
    logger.debug("writing to DB...")
    rag.sync_edges(block.write_roi)
