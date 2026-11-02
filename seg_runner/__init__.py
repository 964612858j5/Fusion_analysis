"""Segmentation engines run in their own subprocess (plan 7.10).

One environment holds all three engines; each run starts one engine process,
feeds it tasks over a JSON-lines protocol and lets it exit, which frees its
GPU memory. The child side (`runner`, `engines`) imports only numpy and the
engine library -- never Qt or the rest of the application -- so it can be
started with `python -m seg_runner.runner` from the repository root.

Used by Step1's pre-segmentation runs and Step2's segment / merge worker.
"""
