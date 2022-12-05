#!/usr/bin/env python
# -*- coding: utf-8 -*-

import glob
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import dask.array as da
import numpy as np
import pandas as pd
import xarray as xr
from fsspec.spec import AbstractFileSystem
from tifffile import TiffFile, TiffFileError, TiffSequence, imread
from tifffile.tifffile import TiffTags

from .. import constants, exceptions, types
from ..dimensions import (
    DEFAULT_CHUNK_DIMS,
    DEFAULT_DIMENSION_ORDER,
    DEFAULT_DIMENSION_ORDER_LIST_WITH_SAMPLES,
    REQUIRED_CHUNK_DIMS,
    DimensionNames,
)
from ..metadata import utils as metadata_utils
from ..utils import io_utils
from .reader import Reader

TIFF_IMAGE_DESCRIPTION_TAG_INDEX = 270


class TiffGlobReader(Reader):
    r"""
    Wraps the tifffile imread API to provide the same aicsimageio Reader API but for
    multifile tiff datasets (and other tifffile supported) images.

    Parameters
    ----------
    glob_in: Union[PathLike, Iterable[PathLike]]
        Glob string that identifies all files to be loaded or an iterable
        of paths to the files as returned by glob.
    indexer: Union[Callable, pandas.DataFrame]
        If callable, should consume each filename and return a pd.Series with series
        index corresponding to the dimensions and values corresponding to the array
        index of that image file within the larger array.
        Default: None (Look for 4 numbers in the file name and use them as
        S, T, C, and Z indices.)
    scene_glob_character: str
        Character to represent different scenes.
        Default: "S"
    chunk_dims: Union[str, List[str]]
        Which dimensions to create chunks for.
        Default: DEFAULT_CHUNK_DIMS
        Note: Dimensions.SpatialY, Dimensions.SpatialX, and DimensionNames.Samples,
        will always be added to the list if not present during dask array
        construction.
    dim_order: Optional[Union[List[str], str]]
        A string of dimensions to be applied to all array(s) or a
        list of string dimension names to be mapped onto the list of arrays
        provided to image. I.E. "TYX".
        Default: None (guess dimensions for single array or multiple arrays)
    channel_names: Optional[Union[List[str], List[List[str]]]]
        A list of string channel names to be applied to all array(s) or a
        list of lists of string channel names to be mapped onto the list of arrays
        provided to image.
        Default: None (create OME channel IDs for names for single or multiple arrays)
    single_file_shape : Optional[Tuple]
        Expected shape for a single file of the set. If not provided, will attempt to
        determine the shape from the first file found in the glob.
        Default : None
    single_file_dims : Optional[Tuple]
        Dimensions that correspond to the data dimensions of a single file in the glob.
        Default : ('Y', 'X')
    fs_kwargs: Dict[str, Any]
        Any specific keyword arguments to pass down to the fsspec created filesystem.
        Default: {}

    Examples
    --------
    # Given files with names like "Position001_t002_c03_z04.tif"

    reader = TiffGlobReader("path/to/data/*.tif")

    # We can use this to read single image tiffs generated by MicroManager
    # Micromanager creates directories for each position so we need to recursively glob
    # for the images files and pass the list to TiffGlobReader. Note that all images are
    # named according to img_channel000_position001_time000000003_z004.tif"

    import glob
    files = glob.glob("path/to/data/**/*.tif", recursive=True)

    # since the numbers in Micromanager files are not in STCZ order we
    # need to use a different indexer than default. For convenience
    # when working MicroManager generated files you can use the provided
    # TiffGlobReader.MicroManagerIndexer

    mm_reader = TiffGlobReader(files, indexer=TiffGlobReader.MicroManagerIndexer)

    # as an example of making a custom indexer you can manually create
    # the MicroManagerIndexer like so:

    import pandas as pd
    from pathlib import Path
    import re
    def mm_indexer(path_to_img):
        inds = re.findall(r"\d+", Path(path_to_img).name)
        series = pd.Series(inds, index=['C', 'S', 'T', 'Z']).astype(int)
        return series

    mm_reader = TiffGlobReader(files, indexer=mm_indexer)
    """

    @staticmethod
    def _is_supported_image(
        fs: AbstractFileSystem, path: types.PathLike, **kwargs: Any
    ) -> bool:
        try:
            with fs.open(path) as open_resource:
                with TiffFile(open_resource):
                    return True

        except (TiffFileError, TypeError):
            return False

    def __init__(
        self,
        glob_in: Union[types.PathLike, Iterable[types.PathLike]],
        indexer: Union[pd.DataFrame, Callable] = None,
        scene_glob_character: str = "S",
        chunk_dims: Union[str, List[str]] = DEFAULT_CHUNK_DIMS,
        dim_order: Optional[Union[List[str], str]] = None,
        channel_names: Optional[Union[List[str], List[List[str]]]] = None,
        single_file_shape: Optional[Tuple] = None,
        single_file_dims: Sequence[str] = (
            DimensionNames.SpatialY,
            DimensionNames.SpatialX,
        ),
        fs_kwargs: Dict[str, Any] = {},
        **kwargs: Any,
    ):

        # Assemble glob list if given a string
        if isinstance(glob_in, str):
            file_series = pd.Series(glob.glob(glob_in))
        elif isinstance(glob_in, Path) and "*" in str(glob_in):
            file_series = pd.Series(glob.glob(str(glob_in)))
        elif isinstance(glob_in, pd.Series):
            # Ensure all of our indices line up
            file_series = glob_in.reset_index(drop=True, inplace=False)
        elif isinstance(glob_in, Iterable):
            file_series = pd.Series(glob_in)
        else:
            raise TypeError(f"Invalid type glob_in - got type {type(glob_in)}")

        if len(file_series) == 0:
            raise ValueError("No files found matching glob pattern")

        self.scene_glob_character = scene_glob_character

        if indexer is None:
            series_idx = [
                self.scene_glob_character,
                DimensionNames.Time,
                DimensionNames.Channel,
                DimensionNames.SpatialZ,
            ]

            # By default we will attempt to parse 4 numbers out of the filename
            # and assign them in order to be the corresponding S, T, C, and Z indices.
            # So indexer("path/to/data/S0_T1_C2_Z3.tif") returns
            # pd.Series([0,1,2,3], index=['S','T','C', 'Z'])
            def indexer(x: str) -> pd.Series:
                return pd.Series(
                    re.findall(r"\d+", Path(x).name), index=series_idx
                ).astype(int)

        if callable(indexer):
            self._all_files = file_series.apply(indexer)
            self._all_files["filename"] = file_series
        elif isinstance(indexer, pd.DataFrame):
            # make a copy of the indexing dataframe and reset it index
            # to ensure that we don't generate nans when aligning with
            # file_series.
            self._all_files = indexer.reset_index(drop=True, inplace=False)
            self._all_files["filename"] = file_series

        # If a dim doesn't exist on the file set the column value for that dim to zero.
        # If the dim is present, add it to the sort order. Because we are using
        # the default dimension ordering, this will naturally create a sort order
        # based off the standard dimension order.
        sort_order = []
        for dim in DEFAULT_DIMENSION_ORDER_LIST_WITH_SAMPLES:
            if dim not in self._all_files.columns and dim not in single_file_dims:
                self._all_files[dim] = 0
            if dim in self._all_files.columns:
                sort_order.append(dim)

        self._all_files = self._all_files.sort_values(sort_order).reset_index(drop=True)

        # run tests on a single file (?)
        self._fs, self._path = io_utils.pathlike_to_fs(
            self._all_files.iloc[0].filename,
            fs_kwargs=fs_kwargs,
        )

        # Store params
        if isinstance(chunk_dims, str):
            self.chunk_dims = list(chunk_dims)
        elif isinstance(chunk_dims, list) and isinstance(chunk_dims[0], str):
            self.chunk_dims = chunk_dims
        else:
            raise ValueError("chunk_dims must be str or list of str")

        # Run basic checks on dims and channel names
        if isinstance(dim_order, list):
            if len(dim_order) != len(self.scenes):
                raise exceptions.ConflictingArgumentsError(
                    f"Number of dimension strings provided does not match the "
                    f"number of scenes found in the file. "
                    f"Number of scenes: {len(self.scenes)}, "
                    f"Number of provided dimension order strings: {len(dim_order)}"
                )

        self._channel_names = channel_names

        # If provided a list
        if isinstance(channel_names, list):
            # If provided a list of lists
            if len(channel_names) > 0 and isinstance(channel_names[0], list):
                # Ensure that the outer list is the number of scenes
                if len(channel_names) != len(self.scenes):
                    raise exceptions.ConflictingArgumentsError(
                        f"Number of channel name lists provided does not match the "
                        f"number of scenes found in the file. "
                        f"Number of scenes: {len(self.scenes)}, "
                        f"Provided channel name lists: {dim_order}"
                    )
            self._channel_names = channel_names

        for dim in REQUIRED_CHUNK_DIMS:
            if dim not in self.chunk_dims:
                self.chunk_dims.append(dim)

        # Safety measure / "feature"
        self.chunk_dims = [d.upper() for d in self.chunk_dims]

        if dim_order is not None:
            self._dim_order = dim_order
        else:
            self._dim_order = "".join(
                d
                for d in DEFAULT_DIMENSION_ORDER
                if d in self._all_files.columns or d in self.chunk_dims
            )

        if single_file_shape is None:
            with self._fs.open(self._path) as open_resource:
                with TiffFile(open_resource) as tiff:
                    self._single_file_shape = tiff.series[0].shape

        else:
            self._single_file_shape = single_file_shape

        if len(single_file_dims) != len(self._single_file_shape):
            raise exceptions.ConflictingArgumentsError(
                f"Number of single file dimensions does not match the"
                f"number of dimensions in a test file. "
                f"Number of dimensions in file: {len(self._single_file_shape)}, "
                f"Provided number of dimensions: {len(single_file_dims)}."
            )

        else:
            self._single_file_dims = list(single_file_dims)

        self._single_file_sizes = dict(
            zip(self._single_file_dims, self._single_file_shape)
        )
        # Enforce valid image
        if not self._is_supported_image(self._fs, self._path):
            raise exceptions.UnsupportedFileFormatError(
                self.__class__.__name__, self._path
            )

    @property
    def scenes(self) -> Tuple[str, ...]:
        if self._scenes is None:
            self._scenes = tuple(
                metadata_utils.generate_ome_image_id(s)
                for s in range(self._all_files[self.scene_glob_character].nunique())
            )
        return self._scenes

    def _read_delayed(self) -> xr.DataArray:

        scene_files = self._all_files.loc[
            self._all_files[self.scene_glob_character] == self.current_scene_index
        ]
        scene_files = scene_files.drop(self.scene_glob_character, axis=1)
        scene_nunique = scene_files.nunique()

        tiff_tags = self._get_tiff_tags(TiffFile(scene_files.filename.iloc[0]))

        group_dims = [
            x for x in scene_files.columns if x not in ["filename", *self.chunk_dims]
        ]

        # xxx_sizes are modeled after xr.DataArray.sizes
        # These are OrderedDicts that map a dimension name to a shape.
        # Use these to align and reshape the arrays that come from imread
        # dims and sizes are not always necessary but they keep things much
        # clearer internally.

        # sizes of dimensions we grouping by i.e. not chunks
        group_sizes = OrderedDict([(d, scene_nunique[d]) for d in group_dims])

        # sizes of each chunk
        chunk_sizes = self._get_chunk_sizes(scene_nunique, group_dims)

        # sizes that will be used to reshape the array representing
        # the full glob into separate dimensions.
        unpack_sizes = OrderedDict(
            [
                (d, s)
                for d, s in scene_nunique.iteritems()
                if d in chunk_sizes.keys() - group_sizes.keys()
            ]
        )
        reshape_sizes = tuple(unpack_sizes.values()) + tuple(
            self._single_file_sizes.values()
        )

        # after unpacking the result of imread we sometimes need to rearrange dims
        # in case they are in the glob and single files.
        axes_order = self._get_axes_order(chunk_sizes, unpack_sizes, group_sizes)

        # expand the sizes with singleton dimensions to facilitate da.block at the end
        expanded_blocks_sizes, expanded_chunk_sizes = self._get_expanded_shapes(
            group_sizes, chunk_sizes
        )

        # Assemble the dask array
        if len(group_dims) > 0:  # use groupby to assemble array out of chunks
            blocks = np.zeros(tuple(group_sizes.values()), dtype="object")
            for i, (idx, val) in enumerate(scene_files.groupby(group_dims)):
                with TiffSequence(val.filename.tolist()) as tif:
                    with tif.aszarr() as zarr_im:
                        darr = da.from_zarr(zarr_im).rechunk(-1)

                # unpack the first dimension if it contains multiple axes
                darr = darr.reshape(reshape_sizes)

                # Then reorder dimensions so matching ones from the glob
                # and the file are adjacent (glob then file)
                darr = darr.transpose(axes_order)

                # Then reshape the array to chunk_sizes
                darr = darr.reshape(tuple(expanded_chunk_sizes.values()))

                blocks[idx] = darr

            blocks = blocks.reshape(tuple(expanded_blocks_sizes.values()))
            d_data = da.block(blocks.tolist())
            dims = list(expanded_blocks_sizes.keys())

        else:  # assemble array in a single chunk
            zarr_im = imread(scene_files.filename.tolist(), aszarr=True, level=0)
            darr = da.from_zarr(zarr_im).rechunk(-1)
            darr = darr.reshape(reshape_sizes)
            darr = darr.transpose(axes_order)
            d_data = darr.reshape(tuple(chunk_sizes.values()))
            dims = list(expanded_chunk_sizes.keys())

        # Assign dims and coords to construct xarray
        channel_names = self._get_channel_names_for_scene(dims, d_data.shape)

        coords = self._get_coords(
            dims, d_data.shape, self.current_scene_index, channel_names
        )

        # Try accepted processed metadata
        try:
            attrs = {
                constants.METADATA_UNPROCESSED: tiff_tags,
                constants.METADATA_PROCESSED: tiff_tags[
                    TIFF_IMAGE_DESCRIPTION_TAG_INDEX
                ],
            }
        except KeyError:
            attrs = {constants.METADATA_UNPROCESSED: tiff_tags}

        x_data = xr.DataArray(d_data, dims=dims, coords=coords, attrs=attrs)

        x_data = x_data.transpose(*self._dim_order)

        return x_data

    def _read_immediate(self) -> xr.DataArray:
        # Set up scene specific information
        scene_files = self._all_files.loc[
            self._all_files[self.scene_glob_character] == self.current_scene_index
        ]
        scene_files = scene_files.drop(self.scene_glob_character, axis=1)
        scene_nunique = scene_files.nunique()

        tiff_tags = self._get_tiff_tags(TiffFile(scene_files.filename.iloc[0]))

        chunk_sizes = self._get_chunk_sizes(scene_nunique)

        unpack_sizes = OrderedDict(
            [(d, s) for d, s in scene_nunique.iteritems() if d in chunk_sizes.keys()]
        )

        reshape_sizes = tuple(unpack_sizes.values()) + tuple(
            self._single_file_sizes.values()
        )

        axes_order = self._get_axes_order(chunk_sizes, unpack_sizes)
        # Assemble array
        arr = imread(scene_files.filename.tolist(), level=0)
        arr = arr.reshape(reshape_sizes)
        arr = arr.transpose(axes_order)
        arr = arr.reshape(tuple(chunk_sizes.values()))

        # Assign dims and coords to construct xarray
        dims = scene_files.columns.drop("filename").values.tolist()
        file_dims = [x for x in self._single_file_dims if x not in dims]
        dims += file_dims

        channel_names = self._get_channel_names_for_scene(dims, arr.shape)

        coords = self._get_coords(
            dims, arr.shape, self.current_scene_index, channel_names
        )

        # Try accepted processed metadata
        try:
            attrs = {
                constants.METADATA_UNPROCESSED: tiff_tags,
                constants.METADATA_PROCESSED: tiff_tags[
                    TIFF_IMAGE_DESCRIPTION_TAG_INDEX
                ],
            }
        except KeyError:
            attrs = {constants.METADATA_UNPROCESSED: tiff_tags}
        x_data = xr.DataArray(
            arr,
            dims=dims,
            coords=coords,
            attrs=attrs,
        )

        return x_data

    def _get_axes_order(
        self,
        chunk_sizes: OrderedDict,
        unpack_sizes: OrderedDict,
        group_sizes: OrderedDict = OrderedDict(),
    ) -> Tuple:
        axes_order: Tuple[int, ...] = ()
        for d in chunk_sizes:
            if d in unpack_sizes:
                axes_order += (list(unpack_sizes.keys()).index(d),)
            if d in self._single_file_sizes:
                axes_order += (
                    len(unpack_sizes) + list(self._single_file_sizes.keys()).index(d),
                )
        return axes_order

    def _get_chunk_sizes(
        self, scene_files_nunique: pd.Series, group_dims: List[str] = []
    ) -> OrderedDict:

        sizes = OrderedDict()
        for i, x in scene_files_nunique.iteritems():
            if i not in ["filename", *group_dims]:
                if i not in self._single_file_dims:
                    sizes[i] = x
                else:
                    sizes[i] = self._single_file_sizes[i] * x

        for d, s in self._single_file_sizes.items():
            if d not in self.chunk_dims and d not in sizes:
                sizes[d] = s

        for i, x in self._single_file_sizes.items():
            if i not in scene_files_nunique.index:
                sizes[i] = x

        return sizes

    def _get_expanded_shapes(
        self, group_sizes: OrderedDict, chunk_sizes: OrderedDict
    ) -> Tuple[OrderedDict, OrderedDict]:
        expanded_blocks_sizes = OrderedDict()
        expanded_chunk_sizes = OrderedDict()

        for i, (d, s) in enumerate(group_sizes.items()):
            if d in chunk_sizes:
                if d not in expanded_blocks_sizes:
                    d_idx_in_chunks = list(chunk_sizes.keys()).index(d)
                    for j in range(d_idx_in_chunks):
                        c_key = list(chunk_sizes.keys())[j]
                        if c_key not in expanded_blocks_sizes:
                            expanded_blocks_sizes[c_key] = 1

                    expanded_blocks_sizes[d] = s

            if d not in chunk_sizes:
                if len(expanded_blocks_sizes) <= i:
                    expanded_blocks_sizes[d] = s
                else:
                    for d2 in expanded_blocks_sizes:
                        expanded_chunk_sizes[d2] = chunk_sizes[d2]
                    expanded_chunk_sizes[d] = 1
                    expanded_blocks_sizes[d] = group_sizes[d]
                    for d2, s2 in chunk_sizes.items():
                        if d2 not in expanded_chunk_sizes:
                            expanded_chunk_sizes[d2] = s2

        for d, s in chunk_sizes.items():
            if d not in expanded_blocks_sizes:
                expanded_blocks_sizes[d] = 1

        if len(expanded_chunk_sizes) == 0:
            expanded_chunk_sizes = chunk_sizes

        return expanded_blocks_sizes, expanded_chunk_sizes

    def _get_channel_names_for_scene(
        self, dims: List[str], image_shape: Tuple[int, ...]
    ) -> Optional[List[str]]:
        # Fast return in None case
        if self._channel_names is None:
            return None

        # If channels was provided as a list of lists
        if isinstance(self._channel_names[0], list):
            scene_channels = self._channel_names[self.current_scene_index]
        elif all(isinstance(c, str) for c in self._channel_names):
            scene_channels = self._channel_names  # type: ignore
        else:
            return None

        # If scene channels isn't None and no channel dimension raise error
        if DimensionNames.Channel not in dims:
            raise exceptions.ConflictingArgumentsError(
                f"Provided channel names for scene with no channel dimension. "
                f"Scene dims: {dims}, "
                f"Provided channel names: {scene_channels}"
            )

        # If scene channels isn't the same length as the size of channel dim
        if len(scene_channels) != image_shape[dims.index(DimensionNames.Channel)]:
            raise exceptions.ConflictingArgumentsError(
                f"Number of channel names provided does not match the "
                f"size of the channel dimension for this scene. "
                f"Scene shape: {image_shape}, "
                f"Dims: {dims}, "
                f"Provided channel names: {self._channel_names}",
            )

        return scene_channels  # type: ignore

    @staticmethod
    def _get_coords(
        dims: List[str],
        shape: Tuple[int, ...],
        scene_index: int,
        channel_names: Optional[List[str]],
    ) -> Dict[str, Any]:
        # Use dims for coord determination
        coords: Dict[str, Any] = {}

        if channel_names is None:
            # Get ImageId for channel naming
            image_id = metadata_utils.generate_ome_image_id(scene_index)

            # Use range for channel indices
            if DimensionNames.Channel in dims:
                coords[DimensionNames.Channel] = [
                    metadata_utils.generate_ome_channel_id(
                        image_id=image_id, channel_id=i
                    )
                    for i in range(shape[dims.index(DimensionNames.Channel)])
                ]
        else:
            coords[DimensionNames.Channel] = channel_names

        return coords

    def _get_tiff_tags(self, tiff: TiffFile) -> TiffTags:
        unprocessed_tags = tiff.series[0].pages[0].tags

        # Create dict of tag and value
        tags: Dict[int, str] = {}
        for code, tag in unprocessed_tags.items():
            tags[code] = tag.value

        return tags

    @staticmethod
    def MicroManagerIndexer(path_to_img: Union[str, Path]) -> pd.Series:
        """
        An indexer function to transform Micromanager MDA tiff filenames
        to indices. To use::

            reader = TiffGlobReader(files, indexer=TiffGlobReader.MicroManagerIndexer)

        Expects images to have names of the form:
            img_channel_[0-9]+_position[0-9]+_time[0-9]+_z[0-9]+.tif[f]

        Parameters
        ----------
        path_to_img : [str, Path]
            The path to an image.

        Returns
        -------
        pd.Series
        """
        inds = re.findall(r"\d+", Path(path_to_img).name)
        series = pd.Series(inds, index=["C", "S", "T", "Z"]).astype(int)
        return series
