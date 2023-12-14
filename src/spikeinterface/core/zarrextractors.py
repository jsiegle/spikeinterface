from __future__ import annotations
from typing import List, Union

from pathlib import Path
from probeinterface import ProbeGroup

import numpy as np

from .baserecording import BaseRecording, BaseRecordingSegment
from .basesorting import BaseSorting, BaseSortingSegment
from .core_tools import define_function_from_class, check_json
from .job_tools import split_job_kwargs

try:
    import zarr

    HAVE_ZARR = True
except ImportError:
    HAVE_ZARR = False


class ZarrRecordingExtractor(BaseRecording):
    """
    RecordingExtractor for a zarr format

    Parameters
    ----------
    root_path: str or Path
        Path to the zarr root file
    storage_options: dict or None
        Storage options for zarr `store`. E.g., if "s3://" or "gcs://" they can provide authentication methods, etc.

    Returns
    -------
    recording: ZarrRecordingExtractor
        The recording Extractor
    """

    extractor_name = "ZarrRecording"
    installed = HAVE_ZARR  # check at class level if installed or not
    mode = "file"
    # error message when not installed
    installation_mesg = "To use the ZarrRecordingExtractor install zarr: \n\n pip install zarr\n\n"
    name = "zarr"

    def __init__(self, root_path: Union[Path, str], storage_options: dict | None = None):
        assert self.installed, self.installation_mesg

        if storage_options is None:
            if isinstance(root_path, str):
                root_path_init = root_path
                root_path = Path(root_path)
            else:
                root_path_init = str(root_path)
            root_path_kwarg = str(Path(root_path).absolute())
        else:
            root_path_init = root_path
            root_path_kwarg = root_path_init

        self._root = zarr.open(root_path_init, mode="r", storage_options=storage_options)

        sampling_frequency = self._root.attrs.get("sampling_frequency", None)
        num_segments = self._root.attrs.get("num_segments", None)
        assert "channel_ids" in self._root.keys(), "'channel_ids' dataset not found!"
        channel_ids = self._root["channel_ids"][:]

        assert sampling_frequency is not None, "'sampling_frequency' attiribute not found!"
        assert num_segments is not None, "'num_segments' attiribute not found!"

        channel_ids = np.array(channel_ids)

        dtype = self._root["traces_seg0"].dtype

        BaseRecording.__init__(self, sampling_frequency, channel_ids, dtype)

        dtype = np.dtype(dtype)
        t_starts = self._root.get("t_starts", None)

        total_nbytes = 0
        total_nbytes_stored = 0
        cr_by_segment = {}
        for segment_index in range(num_segments):
            trace_name = f"traces_seg{segment_index}"
            assert (
                len(channel_ids) == self._root[trace_name].shape[1]
            ), f"Segment {segment_index} has the wrong number of channels!"

            time_kwargs = {}
            time_vector = self._root.get(f"times_seg{segment_index}", None)
            if time_vector is not None:
                time_kwargs["time_vector"] = time_vector
            else:
                if t_starts is None:
                    t_start = None
                else:
                    t_start = t_starts[segment_index]
                    if np.isnan(t_start):
                        t_start = None
                time_kwargs["t_start"] = t_start
                time_kwargs["sampling_frequency"] = sampling_frequency

            rec_segment = ZarrRecordingSegment(self._root, trace_name, **time_kwargs)

            nbytes_segment = self._root[trace_name].nbytes
            nbytes_stored_segment = self._root[trace_name].nbytes_stored
            cr_by_segment[segment_index] = nbytes_segment / nbytes_stored_segment

            total_nbytes += nbytes_segment
            total_nbytes_stored += nbytes_stored_segment
            self.add_recording_segment(rec_segment)

        # load probe
        probe_dict = self._root.attrs.get("probe", None)
        if probe_dict is not None:
            probegroup = ProbeGroup.from_dict(probe_dict)
            self.set_probegroup(probegroup, in_place=True)

        # load properties
        if "properties" in self._root:
            prop_group = self._root["properties"]
            for key in prop_group.keys():
                values = self._root["properties"][key]
                self.set_property(key, values)

        # load annotations
        annotations = self._root.attrs.get("annotations", None)
        if annotations is not None:
            self.annotate(**annotations)
        # annotate compression ratios
        cr = total_nbytes / total_nbytes_stored
        self.annotate(compression_ratio=cr, compression_ratio_segments=cr_by_segment)

        self._kwargs = {"root_path": root_path_kwarg, "storage_options": storage_options}

    @staticmethod
    def write_recording(
        recording: BaseRecording, zarr_path: Union[str, Path], storage_options: dict | None = None, **kwargs
    ):
        zarr_root = zarr.open(str(zarr_path), mode="w", storage_options=storage_options)
        add_recording_to_zarr_group(recording, zarr_root, **kwargs)


class ZarrRecordingSegment(BaseRecordingSegment):
    def __init__(self, root, dataset_name, **time_kwargs):
        BaseRecordingSegment.__init__(self, **time_kwargs)
        self._timeseries = root[dataset_name]

    def get_num_samples(self) -> int:
        """Returns the number of samples in this signal block

        Returns:
            SampleIndex: Number of samples in the signal block
        """
        return self._timeseries.shape[0]

    def get_traces(
        self,
        start_frame: Union[int, None] = None,
        end_frame: Union[int, None] = None,
        channel_indices: Union[List, None] = None,
    ) -> np.ndarray:
        traces = self._timeseries[start_frame:end_frame]
        if channel_indices is not None:
            traces = traces[:, channel_indices]
        return traces


class ZarrSortingExtractor(BaseSorting):
    """
    SortingExtractor for a zarr format

    Parameters
    ----------
    root_path: str or Path
        Path to the zarr root file
    storage_options: dict or None
        Storage options for zarr `store`. E.g., if "s3://" or "gcs://" they can provide authentication methods, etc.

    Returns
    -------
    sorting: ZarrSortingExtractor
        The sorting Extractor
    """

    extractor_name = "ZarrSorting"
    installed = HAVE_ZARR  # check at class level if installed or not
    mode = "file"
    # error message when not installed
    installation_mesg = "To use the ZarrSortingExtractor install zarr: \n\n pip install zarr\n\n"
    name = "zarr"

    def __init__(self, root_path: Union[Path, str], storage_options: dict | None = None):
        assert self.installed, self.installation_mesg

        if storage_options is None:
            if isinstance(root_path, str):
                root_path_init = root_path
                root_path = Path(root_path)
            else:
                root_path_init = str(root_path)
            root_path_kwarg = str(Path(root_path).absolute())
        else:
            root_path_init = root_path
            root_path_kwarg = root_path_init

        self._root = zarr.open(root_path_init, mode="r", storage_options=storage_options)

        sampling_frequency = self._root.attrs.get("sampling_frequency", None)
        num_segments = self._root.attrs.get("num_segments", None)
        assert "unit_ids" in self._root.keys(), "'unit_ids' dataset not found!"
        unit_ids = self._root["unit_ids"][:]

        assert sampling_frequency is not None, "'sampling_frequency' attiribute not found!"
        assert num_segments is not None, "'num_segments' attiribute not found!"

        unit_ids = np.array(unit_ids)
        assert "spikes" in self._root.keys(), "'spikes' dataset not found!"
        spikes_group = self._root["spikes"]
        segment_slices_list = spikes_group["segment_slices"][:]
        segment_slices = [slice(s[0], s[1]) for s in segment_slices_list]

        BaseSorting.__init__(self, sampling_frequency, unit_ids)

        for segment_index in range(num_segments):
            soring_segment = ZarrSortingSegment(spikes_group, segment_slices[segment_index], unit_ids)
            self.add_sorting_segment(soring_segment)

        # load properties
        if "properties" in self._root:
            prop_group = self._root["properties"]
            for key in prop_group.keys():
                values = self._root["properties"][key]
                self.set_property(key, values)

        # load annotations
        annotations = self._root.attrs.get("annotations", None)
        if annotations is not None:
            self.annotate(**annotations)

        self._kwargs = {"root_path": root_path_kwarg, "storage_options": storage_options}

    @staticmethod
    def write_sorting(sorting: BaseSorting, zarr_path: Union[str, Path], storage_options: dict | None = None, **kwargs):
        """
        Write a sorting extractor to zarr format.
        """
        zarr_root = zarr.open(str(zarr_path), mode="w", storage_options=storage_options)
        add_sorting_to_zarr_group(sorting, zarr_root, **kwargs)


class ZarrSortingSegment(BaseSortingSegment):
    def __init__(self, spikes_dset, segment_slice, unit_ids):
        BaseSortingSegment.__init__(self)
        self._spikes_indices = spikes_dset["sample_index"][segment_slice]
        self._unit_indices = spikes_dset["unit_index"][segment_slice]
        self._unit_ids = list(unit_ids)

    def get_unit_spike_train(
        self,
        unit_id,
        start_frame: Union[int, None] = None,
        end_frame: Union[int, None] = None,
    ) -> np.ndarray:
        start = 0 if start_frame is None else np.searchsorted(self._spikes_indices, start_frame)
        end = len(self._spikes_indices) if end_frame is None else np.searchsorted(self._spikes_indices, end_frame)
        sample_indices = self._spikes_indices[start:end]
        unit_indices = self._unit_indices[start:end]
        unit_index = self._unit_ids.index(unit_id)
        return sample_indices[unit_indices == unit_index]


read_zarr_recording = define_function_from_class(source_class=ZarrRecordingExtractor, name="read_zarr_recording")
read_zarr_sorting = define_function_from_class(source_class=ZarrSortingExtractor, name="read_zarr_sorting")


def read_zarr(
    root_path: Union[str, Path], storage_options: dict | None = None
) -> Union[ZarrRecordingExtractor, ZarrSortingExtractor]:
    """
    Read recording or sorting from a zarr format

    Parameters
    ----------
    root_path: str or Path
        Path to the zarr root file
    storage_options: dict or None
        Storage options for zarr `store`. E.g., if "s3://" or "gcs://" they can provide authentication methods, etc.

    Returns
    -------
    extractor: ZarrExtractor
        The loaded extractor
    """
    if storage_options is None:
        if isinstance(root_path, str):
            root_path_init = root_path
            root_path = Path(root_path)
        else:
            root_path_init = str(root_path)
    else:
        root_path_init = root_path

    root = zarr.open(root_path_init, mode="r", storage_options=storage_options)
    if "channel_ids" in root.keys():
        return read_zarr_recording(root_path, storage_options=storage_options)
    elif "unit_ids" in root.keys():
        return read_zarr_sorting(root_path, storage_options=storage_options)
    else:
        raise ValueError("Cannot find 'channel_ids' or 'unit_ids' in zarr root. Not a valid SpikeInterface zarr format")


### UTILITY FUNCTIONS ###
def get_default_zarr_compressor(clevel=5):
    """
    Return default Zarr compressor object for good preformance in int16
    electrophysiology data.

    cname: zstd (zstandard)
    clevel: 5
    shuffle: BITSHUFFLE

    Parameters
    ----------
    clevel : int, default: 5
        Compression level (higher -> more compressed).
        Minimum 1, maximum 9. By default 5

    Returns
    -------
    Blosc.compressor
        The compressor object that can be used with the save to zarr function
    """
    assert ZarrRecordingExtractor.installed, ZarrRecordingExtractor.installation_mesg
    from numcodecs import Blosc

    return Blosc(cname="zstd", clevel=clevel, shuffle=Blosc.BITSHUFFLE)


def add_properties_and_annotations(
    zarr_root: zarr.hierarchy.Group, recording_or_sorting: Union[BaseRecording, BaseSorting]
):
    # save properties
    prop_group = zarr_root.create_group("properties")
    for key in recording_or_sorting.get_property_keys():
        values = recording_or_sorting.get_property(key)
        prop_group.create_dataset(name=key, data=values, compressor=None)

    # save annotations
    zarr_root.attrs["annotations"] = check_json(recording_or_sorting._annotations)


def add_sorting_to_zarr_group(sorting: BaseSorting, zarr_group: zarr.hierarchy.Group, **kwargs):
    """
    Add a sorting extractor to a zarr group.

    Parameters
    ----------
    sorting: BaseSorting
        The sorting extractor object to be added to the zarr group
    zarr_group: zarr.hierarchy.Group
        The zarr group
    kwargs: dict
        Other arguments passed to the zarr compressor
    """
    from numcodecs import Delta

    num_segments = sorting.get_num_segments()
    zarr_group.attrs["sampling_frequency"] = float(sorting.sampling_frequency)
    zarr_group.attrs["num_segments"] = int(num_segments)
    zarr_group.create_dataset(name="unit_ids", data=sorting.unit_ids, compressor=None)

    if "compressor" not in kwargs:
        compressor = get_default_zarr_compressor()

    # save sub fields
    spikes_group = zarr_group.create_group(name="spikes")
    spikes = sorting.to_spike_vector()
    for field in spikes.dtype.fields:
        if field != "segment_index":
            spikes_group.create_dataset(
                name=field,
                data=spikes[field],
                compressor=compressor,
                filters=[Delta(dtype=spikes[field].dtype)],
            )
        else:
            segment_slices = []
            for segment_index in range(num_segments):
                i0, i1 = np.searchsorted(spikes["segment_index"], [segment_index, segment_index + 1])
                segment_slices.append([i0, i1])
            spikes_group.create_dataset(name="segment_slices", data=segment_slices, compressor=None)

    add_properties_and_annotations(zarr_group, sorting)


# Recording
def add_recording_to_zarr_group(recording: BaseRecording, zarr_group: zarr.hierarchy.Group, **kwargs):
    zarr_kwargs, job_kwargs = split_job_kwargs(kwargs)

    if recording.check_if_json_serializable():
        zarr_group.attrs["provenance"] = check_json(recording.to_dict(recursive=True))
    else:
        zarr_group.attrs["provenance"] = None

    # save data (done the subclass)
    zarr_group.attrs["sampling_frequency"] = float(recording.get_sampling_frequency())
    zarr_group.attrs["num_segments"] = int(recording.get_num_segments())
    zarr_group.create_dataset(name="channel_ids", data=recording.get_channel_ids(), compressor=None)
    dataset_paths = [f"traces_seg{i}" for i in range(recording.get_num_segments())]

    zarr_kwargs["dtype"] = kwargs.get("dtype", None) or recording.get_dtype()
    if "compressor" not in zarr_group:
        zarr_kwargs["compressor"] = get_default_zarr_compressor()

    add_traces_to_zarr(
        recording=recording,
        zarr_group=zarr_group,
        dataset_paths=dataset_paths,
        **zarr_kwargs,
        **job_kwargs,
    )

    # # save probe
    if recording.get_property("contact_vector") is not None:
        probegroup = recording.get_probegroup()
        zarr_group.attrs["probe"] = check_json(probegroup.to_dict(array_as_list=True))

    # save time vector if any
    t_starts = np.zeros(recording.get_num_segments(), dtype="float64") * np.nan
    for segment_index, rs in enumerate(recording._recording_segments):
        d = rs.get_times_kwargs()
        time_vector = d["time_vector"]
        if time_vector is not None:
            _ = zarr_group.create_dataset(
                name=f"times_seg{segment_index}",
                data=time_vector,
                filters=zarr_kwargs.get("filters", None),
                compressor=zarr_kwargs["compressor"],
            )
        elif d["t_start"] is not None:
            t_starts[segment_index] = d["t_start"]

    if np.any(~np.isnan(t_starts)):
        zarr_group.create_dataset(name="t_starts", data=t_starts, compressor=None)

    add_properties_and_annotations(zarr_group, recording)


def add_traces_to_zarr(
    recording,
    zarr_group,
    dataset_paths,
    channel_chunk_size=None,
    dtype=None,
    compressor=None,
    filters=None,
    verbose=False,
    auto_cast_uint=True,
    **job_kwargs,
):
    """
    Save the trace of a recording extractor in several zarr format.

    Parameters
    ----------
    recording: RecordingExtractor
        The recording extractor object to be saved in .dat format
    zarr_group: zarr.Group
        The zarr group to add traces to
    dataset_paths: list
        List of paths to traces datasets in the zarr group
    channel_chunk_size: int or None, default: None (chunking in time only)
        Channels per chunk
    dtype: dtype, default: None
        Type of the saved data
    compressor: zarr compressor or None, default: None
        Zarr compressor
    filters: list, default: None
        List of zarr filters
    verbose: bool, default: False
        If True, output is verbose (when chunks are used)
    auto_cast_uint: bool, default: True
        If True, unsigned integers are automatically cast to int if the specified dtype is signed
    {}
    """
    from .job_tools import (
        ensure_chunk_size,
        fix_job_kwargs,
        ChunkRecordingExecutor,
    )
    from .core_tools import determine_cast_unsigned

    assert dataset_paths is not None, "Provide 'file_path'"

    if not isinstance(dataset_paths, list):
        dataset_paths = [dataset_paths]
    assert len(dataset_paths) == recording.get_num_segments()

    if dtype is None:
        dtype = recording.get_dtype()
    if auto_cast_uint:
        cast_unsigned = determine_cast_unsigned(recording, dtype)
    else:
        cast_unsigned = False

    job_kwargs = fix_job_kwargs(job_kwargs)
    chunk_size = ensure_chunk_size(recording, **job_kwargs)

    # create zarr datasets files
    zarr_datasets = []
    for segment_index in range(recording.get_num_segments()):
        num_frames = recording.get_num_samples(segment_index)
        num_channels = recording.get_num_channels()
        dset_name = dataset_paths[segment_index]
        shape = (num_frames, num_channels)
        dset = zarr_group.create_dataset(
            name=dset_name,
            shape=shape,
            chunks=(chunk_size, channel_chunk_size),
            dtype=dtype,
            filters=filters,
            compressor=compressor,
        )
        zarr_datasets.append(dset)
        # synchronizer=zarr.ThreadSynchronizer())

    # use executor (loop or workers)
    func = _write_zarr_chunk
    init_func = _init_zarr_worker
    init_args = (recording, zarr_datasets, dtype, cast_unsigned)
    executor = ChunkRecordingExecutor(
        recording, func, init_func, init_args, verbose=verbose, job_name="write_zarr_recording", **job_kwargs
    )
    executor.run()


# used by write_zarr_recording + ChunkRecordingExecutor
def _init_zarr_worker(recording, zarr_datasets, dtype, cast_unsigned):
    import zarr

    # create a local dict per worker
    worker_ctx = {}
    worker_ctx["recording"] = recording
    worker_ctx["zarr_datasets"] = zarr_datasets
    worker_ctx["dtype"] = np.dtype(dtype)
    worker_ctx["cast_unsigned"] = cast_unsigned

    return worker_ctx


# used by write_zarr_recording + ChunkRecordingExecutor
def _write_zarr_chunk(segment_index, start_frame, end_frame, worker_ctx):
    import gc

    # recover variables of the worker
    recording = worker_ctx["recording"]
    dtype = worker_ctx["dtype"]
    zarr_dataset = worker_ctx["zarr_datasets"][segment_index]
    cast_unsigned = worker_ctx["cast_unsigned"]

    # apply function
    traces = recording.get_traces(
        start_frame=start_frame, end_frame=end_frame, segment_index=segment_index, cast_unsigned=cast_unsigned
    )
    traces = traces.astype(dtype)
    zarr_dataset[start_frame:end_frame, :] = traces

    # fix memory leak by forcing garbage collection
    del traces
    gc.collect()
