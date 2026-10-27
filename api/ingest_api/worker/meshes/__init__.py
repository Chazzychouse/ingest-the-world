"""Meshes, for the derive stage: reading a model's geometry (`gltf`), holding it (`model`),
simplifying it (`clustering`), and cutting it into 3D Tiles (`tiler`, `textures`, `archive`).

Everything here runs inside a render's sandbox (ING-8) on the original's local copy, and holds
at most one chunk of the file and one tile's geometry in memory, whatever the model's size.
"""
