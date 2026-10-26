"""Mesmer run by PyTorch (plan docs/mesmer_torch_plan.md): the saved
MultiplexSegmentation network re-written as a functional forward pass, its
weights converted once from the Keras model, and the DeepCell pre/post
processing reached through deepcell_toolbox (which needs no TensorFlow).
Licence: see NOTICE and LICENSE-deepcell in this directory."""
