"""Model evaluation: unified model registry, evaluation, explainability, monitoring and governance (algorithm ``mo-1``).

Domain model logic stays in ``forecast``, ``leadtime`` and ``risk``; this package reads their persisted runs and records,
re-computes evidence with one metric library, and governs model versions. It never trains, replaces or deploys a model.
"""
