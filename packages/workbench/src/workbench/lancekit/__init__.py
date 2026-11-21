"""The dataset plane a client serves from: the registry of datasets it opens, their descriptors, keys and predicates.

It reaches a catalog through the stock Lance Namespace SDK (:mod:`workbench.lancekit.catalog`) and decodes an Arrow
body only through ``workbench.arrow_ipc``. Standalone by
contract: nothing here imports a workload's code, so a client carries no modality's dependencies.
"""
