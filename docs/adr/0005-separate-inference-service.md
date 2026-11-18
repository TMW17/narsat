# 0005 - The model runs in a separate inference service

**Context.** Prithvi-EO-2.0 needs PyTorch and TerraTorch (about 140 packages, several GB)
and benefits from a GPU. Docker on macOS has no access to the Apple GPU, but a native
process can use it through MPS.

**Decision.** A small FastAPI service (`services/inference`) with a narrow contract:
a NumPy array of six reflectance bands in, a probability raster out. It tiles large
inputs with overlap and blends them, records the commit of the weights it loaded as the
model version, and loads the checkpoint strictly. A weight-free spectral engine with the
same contract exists for tests and machines that cannot hold the model.

**Consequences.** The pipeline does not depend on PyTorch; the model can run natively on
a Mac or on a GPU machine. Arrays travel over HTTP (a 1024 x 1024 cube is 24 MB),
acceptable for one event at a time.
