# 0006 - Two burned-area methods, always compared

**Context.** The foundation model was fine-tuned on burn scars in the United States. Its
accuracy on Mediterranean forest, maquis and steppe is unknown. Presenting its output as
the burned area would be a claim the project cannot support.

**Decision.** Every mapped event gets a dNBR product (a well-understood spectral index
with an automatic, bounded threshold). When the inference service is available it also
gets a model product, post-processed with the same rules. The agreement between the two
(IoU, precision, recall) and with reference perimeters, when loaded, is stored and shown
next to the areas. Statistics report the two methods separately and never add them.

**Consequences.** Twice the processing per event and a more complex interface; in
exchange, every figure the platform shows can be traced to a method and checked.
Measuring which method is better in the region is left to a benchmark against reference
perimeters (see the roadmap), not asserted.
