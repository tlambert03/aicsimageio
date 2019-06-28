# AICSImageIO

[![build status](https://travis-ci.com/AllenCellModeling/aicsimageio.svg?branch=master)](https://travis-ci.com/AllenCellModeling/aicsimageio)
[![codecov](https://codecov.io/gh/AllenCellModeling/aicsimageio/branch/master/graph/badge.svg)](https://codecov.io/gh/AllenCellModeling/aicsimageio)

A Python library for reading and writing image data with specific support for handling bio-formats.

---

## Features
* Supports reading metadata and imaging data from file path or buffered bytes for:
    * `CZI`
    * `OME-TIFF`
    * `TIFF`
    * Any additional format supported by `imageio`
* Supports writing metadata and imaging data for:
    * `OME-TIFF`
    * `TIFF`
    * Any additional format supported by `imageio`

### Disclaimer:
This package is under heavy revision in preparation for version 3.0.0 release. The quick start below is representative
of how to interact with the package under 3.0.0 and not under the current stable release.

## Quick Start
```
from aicsimageio import AICSImage, imread

# For numpy array of image data
im = imread("/path/to/your/file_or_buffer.ome.tiff")

# For AICSImage object that
im = AICSImage("/path/to/your/file_or_buffer.ome.tiff")

# Image data is stored in `data` attribute
im.data  # returns the image data numpy array

# Image metadata is stored in `metadata` attribute
im.metadata  # returns whichever metadata parser best suites the file format
```

## Notes
* Image data numpy arrays are always returned as six dimensional in dimension order `STCZYX`
or `Scene`, `Time`, `Channel`, `Z`, `Y`, and `X`.
* Each file format may use a different metadata parser it is dependent on the reader's implementation.

## Installation
**Stable Release:** `pip install aicsimageio`<br>
**Development Head:** `pip install git+https://github.com/AllenCellModeling/aicsimageio.git`

## Development
See [CONTRIBUTING.md](CONTRIBUTING.md) for information related to developing the code.

***Free software: Allen Institute Software License***
