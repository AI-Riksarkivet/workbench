"""The dataset plane a client serves from: the registry of datasets it opens, their descriptors, keys and predicates.

It reaches the catalog through the stock Lance Namespace SDK (:mod:`client_kit.lancekit.catalog`) and the platform package's
guard (``service_kit.arrow_ipc``), never through the lakehouse's own kernel. Standalone by
contract: nothing here imports a workload's code, so a client carries no modality's dependencies.
"""
