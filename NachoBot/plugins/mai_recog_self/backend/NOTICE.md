# Source and licensing

- The whole-image preprocessing, similarity grouping, MobileNetV3-Small classifier, training schedule, validation selection, checkpoint/resume format, and audit logic are adapted from MaiSelfPlugin by SengokuCola, GPL-3.0-or-later. The source checkout used for this adaptation was commit `22ab1d241a4c6b474bc9013cc701e01bb3b5fe87` from <https://github.com/SengokuCola/MaiSelfPlugin>.
- The bundled `mobilenet_v3_small-047dcff4.pth` is the PyTorch ImageNet MobileNetV3-Small weights file. Its SHA256 is `047dcff4addef86ea5bc2eff13c9614dc11f47ab1160d0a71a25e7db994f4e1f`.
- The architecture implementation is provided at runtime by torchvision. Worker dependency terms remain with their respective upstream projects.
- No personal images, labels, trained identity model, or experiment results were copied from the upstream checkout. All runtime imports copy user-selected static images into this data store; they never move or delete the source files.

The full GPL-3.0-or-later license text is in `LICENSE`.
