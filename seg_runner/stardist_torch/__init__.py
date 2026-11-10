"""StarDist's network on PyTorch (docs/stardist_torch_plan.md).

The StarDist model keeps running as `stardist.models.StarDist2D`; only its
Keras network is replaced by `network.TorchNetwork`. Normalisation, the pad
and crop, the sparse non-maximum suppression and the polygon reconstruction
stay the stardist package's own code, so the one thing that differs between
the TensorFlow and the PyTorch run is the network.

The weights are the same `weights_best.h5`, converted once by `convert` into a
`.pt` file (never committed). `graph` is the forward pass, `weights` the strict
loader, `numerics` the settings of the network pass.

The architecture is StarDist/CSBDeep's (BSD-3-Clause); the code here is ours.
See NOTICE, LICENSE-stardist and LICENSE-csbdeep in this directory.
"""
