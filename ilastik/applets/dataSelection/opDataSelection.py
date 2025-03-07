###############################################################################
#   ilastik: interactive learning and segmentation toolkit
#
#       Copyright (C) 2011-2014, the ilastik developers
#                                <team@ilastik.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# In addition, as a special exception, the copyright holders of
# ilastik give you permission to combine ilastik with applets,
# workflows and plugins which are not covered under the GNU
# General Public License.
#
# See the LICENSE file for details. License information is also available
# on the ilastik web site at:
#          http://ilastik.org/license.html
###############################################################################
from abc import abstractproperty, abstractmethod, ABC
import glob
import os
import uuid
from typing import List, Tuple, Dict, Optional, Union, Callable
from numbers import Number
import re
from pathlib import Path
import errno

import numpy
import vigra
from vigra import AxisTags
import h5py
import z5py
from ndstructs import Shape5D

from lazyflow.graph import Operator, InputSlot, OutputSlot, OperatorWrapper
from lazyflow.operators.ioOperators import OpStreamingH5N5Reader
from lazyflow.operators.ioOperators import OpInputDataReader
from lazyflow.operators.valueProviders import OpMetadataInjector
from lazyflow.operators.opArrayPiper import OpArrayPiper
from ilastik.applets.base.applet import DatasetConstraintError

from ilastik import Project
from ilastik.utility import OpMultiLaneWrapper
from ilastik.workflow import Workflow
from lazyflow.utility.pathHelpers import splitPath, globH5N5, globNpz, PathComponents
from lazyflow.utility.helpers import get_default_axisordering
from lazyflow.operators.opReorderAxes import OpReorderAxes
from lazyflow.operators import OpMissingDataSource
from lazyflow.operators.ioOperators import OpH5N5WriterBigDataset
from lazyflow.graph import Graph, Operator


def getTypeRange(numpy_type):
    type_info = numpy.iinfo(numpy_type)
    return (type_info.min, type_info.max)


class CantSaveAsRelativePathsException(Exception):
    def __init__(self, file_path: str, base_dir: str):
        super().__init__(f"Can't represent {file_path} relative to {base_dir}")


class UnsuitedAxistagsException(Exception):
    def __init__(self, axistags, shape):
        super().__init__(f"Axistags {axistags} don't fit data shape {shape}")


class DatasetInfo(ABC):
    def __init__(
        self,
        *,
        laneShape: Tuple,
        laneDtype: type,
        default_tags: AxisTags,
        allowLabels: bool = True,
        subvolume_roi: Tuple = None,
        axistags: AxisTags = None,
        display_mode: str = "default",
        nickname: str = "",
        normalizeDisplay: bool = None,
        drange: Tuple[Number, Number] = None,
    ):
        self.laneShape = laneShape
        self.laneDtype = laneDtype
        if isinstance(self.laneDtype, numpy.dtype):
            self.laneDtype = numpy.typeDict[self.laneDtype.name]
        self.allowLabels = allowLabels
        self.subvolume_roi = subvolume_roi
        self.axistags = axistags
        self.drange = drange
        self.display_mode = display_mode  # choices: default, grayscale, rgba, random-colortable, binary-mask.
        self.nickname = nickname
        self.normalizeDisplay = (self.drange is not None) if normalizeDisplay is None else normalizeDisplay
        self.default_tags = default_tags
        self.axistags = axistags or default_tags
        if len(self.axistags) != len(self.laneShape):
            raise UnsuitedAxistagsException(self.axistags, self.laneShape)
        self.legacy_datasetId = self.generate_id()

    @property
    def shape5d(self) -> Shape5D:
        return Shape5D(**dict(zip(self.axiskeys, self.laneShape)))

    @abstractproperty
    def legacy_location(self) -> str:
        pass

    def get_provider_slot(
        self, meta: Optional[Dict] = None, parent: Optional[Operator] = None, graph: Optional[Graph] = None
    ) -> OutputSlot:
        metadata = {"display_mode": self.display_mode, "axistags": self.axistags}
        metadata.update(meta or {})

        if self.drange is not None:
            metadata["drange"] = self.drange
        elif self.laneDtype == numpy.uint8:
            metadata["drange"] = (0, 255)
        if self.normalizeDisplay is not None:
            metadata["normalizeDisplay"] = self.normalizeDisplay
        if self.subvolume_roi is not None:
            metadata["subvolume_roi"] = self.subvolume_roi

        opMetadataInjector = OpMetadataInjector(parent=parent, graph=graph)
        opMetadataInjector.Input.connect(self.get_non_transposed_provider_slot(parent=parent, graph=graph))
        opMetadataInjector.Metadata.setValue(metadata)
        return opMetadataInjector.Output

    @abstractmethod
    def get_non_transposed_provider_slot(
        self, parent: Optional[Operator] = None, graph: Optional[Graph] = None
    ) -> OutputSlot:
        """Gets an OutputSlot which can be queried for the data of this DatasetInfo.

        Like with operators, either parent or graph must be provided, but not both"""
        pass

    def to_json_data(self) -> Dict:
        return {
            "axistags": self.axistags.toJSON().encode("utf-8"),
            "shape": self.laneShape,
            "allowLabels": self.allowLabels,
            "subvolume_roi": self.subvolume_roi,
            "display_mode": self.display_mode.encode("utf-8"),
            "nickname": self.nickname.encode("utf-8"),
            "normalizeDisplay": self.normalizeDisplay,
            "drange": self.drange,
            "location": self.legacy_location.encode("utf-8"),  # legacy support
            "filePath": self.effective_path.encode("utf-8"),  # legacy support
            "datasetId": self.legacy_datasetId.encode("utf-8"),  # legacy support
            "__class__": self.__class__.__name__.encode("utf-8"),
        }

    @classmethod
    def from_h5_group(cls, data: h5py.Group, params: Dict = None):
        params = params or {}
        params.update({"allowLabels": data["allowLabels"][()], "nickname": data["nickname"][()].decode("utf-8")})
        if "axistags" in data:
            params["axistags"] = AxisTags.fromJSON(data["axistags"][()].decode("utf-8"))
        elif "axisorder" in data:  # legacy support
            axisorder = data["axisorder"][()].decode("utf-8")
            params["axistags"] = vigra.defaultAxistags(axisorder)

        if "subvolume_roi" in data:
            params["subvolume_roi"] = tuple(data["subvolume_roi"][()])
        if "normalizeDisplay" in data:
            params["normalizeDisplay"] = bool(data["normalizeDisplay"][()])
        if "drange" in data:
            params["drange"] = tuple(data["drange"])
        if "display_mode" in data:
            params["display_mode"] = data["display_mode"][()].decode("utf-8")
        return cls(**params)

    def is_in_filesystem(self) -> bool:
        return False

    def is_hierarchical(self):
        return False

    @abstractproperty
    def display_string(self) -> str:
        pass

    @property
    def default_output_dir(self) -> Path:
        return Path.home()

    @classmethod
    def create_nickname(cls, expanded_paths: List[str]):
        components = [PathComponents(ep) for ep in expanded_paths]
        external_nickname = os.path.commonprefix(
            [re.sub(comp.extension + "$", "", comp.externalPath) for comp in components]
        )
        if external_nickname:
            external_nickname = Path(external_nickname).name
        else:
            external_nickname = "stack_at-" + components[0].filenameBase
        internal_nickname = os.path.commonprefix([comp.internalPath or "" for comp in components]).lstrip("/")
        nickname = external_nickname + ("-" + internal_nickname.replace("/", "-") if internal_nickname else "")
        return nickname

    @classmethod
    def generate_id(cls) -> str:
        return str(uuid.uuid1())

    @classmethod
    def expand_path(cls, file_path: str, cwd: str = None) -> List[str]:
        """Expands path with globs and colons into a list of absolute paths"""
        cwd = Path(cwd) if cwd else Path.cwd()
        pathComponents = [PathComponents(path) for path in splitPath(file_path)]
        expanded_paths = []
        missing_files = []
        for components in pathComponents:
            externalPath = cwd / Path(components.externalPath).expanduser()
            unglobbed_paths = glob.glob(str(externalPath))
            if not unglobbed_paths:
                missing_files.append(components.externalPath)
                continue
            for ext_path in unglobbed_paths:
                if not cls.fileHasInternalPaths(ext_path) or not components.internalPath:
                    expanded_paths.append(ext_path)
                    continue
                internal_paths = cls.globInternalPaths(ext_path, components.internalPath)
                expanded_paths.extend([os.path.join(ext_path, int_path) for int_path in internal_paths])

        if missing_files:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), os.path.pathsep.join(missing_files))
        return sorted(p.replace("\\", "/") for p in expanded_paths)

    @classmethod
    def globInternalPaths(cls, file_path: str, glob_str: str, cwd: str = None) -> List[str]:
        glob_str = glob_str.lstrip("/")
        internal_paths = set()
        for path in cls.expand_path(file_path, cwd=cwd):
            f = None
            try:
                if cls.pathIsNpz(path):
                    internal_paths |= set(globNpz(path, glob_str))
                    continue
                elif cls.pathIsHdf5(path):
                    f = h5py.File(path, "r")
                elif cls.pathIsN5(path):
                    f = z5py.N5File(path)  # FIXME
                else:
                    raise Exception(f"{path} is not an 'n5' or 'h5' file")
                internal_paths |= set(globH5N5(f, glob_str))
            finally:
                if f is not None:
                    f.close()
        return sorted(internal_paths)

    @classmethod
    def pathIsHdf5(cls, path: Path) -> bool:
        return PathComponents(Path(path).as_posix()).extension in [".ilp", ".h5", ".hdf5"]

    @classmethod
    def pathIsNpz(cls, path: Path) -> bool:
        return PathComponents(Path(path).as_posix()).extension in [".npz"]

    @classmethod
    def pathIsN5(cls, path: Path) -> bool:
        return PathComponents(Path(path).as_posix()).extension in [".n5"]

    @classmethod
    def fileHasInternalPaths(cls, path: str) -> bool:
        return cls.pathIsHdf5(path) or cls.pathIsN5(path) or cls.pathIsNpz(path)

    @classmethod
    def getPossibleInternalPathsFor(cls, file_path: Path, min_ndim=2, max_ndim=5) -> List[str]:
        datasetNames = []

        def accumulateInternalPaths(name, val):
            if isinstance(val, (h5py.Dataset, z5py.dataset.Dataset)) and min_ndim <= len(val.shape) <= max_ndim:
                datasetNames.append("/" + name)

        if cls.pathIsHdf5(file_path):
            with h5py.File(file_path, "r") as f:
                f.visititems(accumulateInternalPaths)
        elif cls.pathIsN5(file_path):
            with z5py.N5File(file_path, mode="r+") as f:
                f.visititems(accumulateInternalPaths)

        return datasetNames

    @property
    def axiskeys(self):
        return "".join(tag.key for tag in self.axistags)

    def __str__(self):
        return str(self.__dict__)

    def importAsLocalDataset(
        self, project_file: h5py.File, progress_signal: Callable[[int], None] = lambda x: None
    ) -> str:
        project = Project(project_file)
        inner_path = project.local_data_group.name + "/" + self.legacy_datasetId
        if project_file.get(inner_path) is not None:
            return inner_path
        self.dumpToHdf5(h5_file=project_file, inner_path=inner_path, progress_signal=progress_signal)
        return inner_path

    def dumpToHdf5(
        self, h5_file: h5py.File, inner_path: str, progress_signal: Callable[[int], None] = lambda x: None
    ) -> str:
        progress_signal(0)
        try:
            h5_file.require_group(Path("/").joinpath(inner_path).parent.as_posix())
            graph = Graph()
            op_writer = OpH5N5WriterBigDataset(
                graph=graph,
                h5N5File=h5_file,
                h5N5Path=inner_path,
                CompressionEnabled=False,
                BatchSize=1,
                Image=self.get_provider_slot(graph=graph),
            )
            op_writer.progressSignal.subscribe(progress_signal)
            success = op_writer.WriteImage.value  # reading this slot triggers the write
        finally:
            progress_signal(100)

    def is_stack(self) -> bool:
        return len(splitPath(self.effective_path)) > 1


class ProjectInternalDatasetInfo(DatasetInfo):
    def __init__(self, *, inner_path: str, project_file: h5py.File, nickname: str = "", **info_kwargs):
        self.inner_path = inner_path
        self.project_file = project_file
        self.dataset = project_file[inner_path]
        if "axistags" in self.dataset.attrs:
            default_tags = vigra.AxisTags.fromJSON(self.dataset.attrs["axistags"])
        else:
            default_tags = vigra.defaultAxistags(get_default_axisordering(self.dataset.shape))
        super().__init__(
            default_tags=default_tags,
            laneShape=self.dataset.shape,
            laneDtype=self.dataset.dtype,
            nickname=nickname or os.path.split(self.inner_path)[-1],
            **info_kwargs,
        )
        self.legacy_datasetId = Path(inner_path).name

    @property
    def legacy_location(self) -> str:
        return "ProjectInternal"

    def to_json_data(self) -> Dict:
        out = super().to_json_data()
        out["inner_path"] = self.inner_path.encode("utf-8")
        out["fromstack"] = True  # legacy support
        return out

    @classmethod
    def from_h5_group(cls, data: h5py.Group, params: Dict = None):
        params = params or {}
        params["project_file"] = data.file
        if "datasetId" in data and "inner_path" not in data:  # legacy format
            dataset_id = data["datasetId"][()].decode("utf-8")
            inner_path = None

            def grab_inner_path(h5_path, dataset):
                nonlocal inner_path
                if h5_path.endswith(dataset_id):
                    inner_path = h5_path

            data.file.visititems(grab_inner_path)
            params["inner_path"] = inner_path
        else:
            params["inner_path"] = data["inner_path"][()].decode("utf-8")
        return super().from_h5_group(data, params)

    @property
    def effective_path(self):
        return self.inner_path

    @property
    def display_string(self) -> str:
        return "Project Internal: " + self.inner_path

    def get_non_transposed_provider_slot(
        self, parent: Optional[Operator] = None, graph: Optional[Graph] = None
    ) -> OutputSlot:
        opReader = OpStreamingH5N5Reader(parent=parent, graph=graph)
        opReader.H5N5File.setValue(self.project_file)
        opReader.InternalPath.setValue(self.inner_path)
        return opReader.OutputImage

    @property
    def internal_paths(self) -> List[str]:
        return []

    @property
    def default_output_dir(self) -> Path:
        return Path(self.project_file.filename).parent


class PreloadedArrayDatasetInfo(DatasetInfo):
    def __init__(self, *, preloaded_array: numpy.ndarray, axistags: AxisTags = None, nickname: str = "", **info_kwargs):
        self.preloaded_array = vigra.taggedView(
            preloaded_array, axistags or get_default_axisordering(preloaded_array.shape)
        )
        super().__init__(
            nickname=nickname or "preloaded-{}-array".format(self.preloaded_array.dtype.name),
            default_tags=self.preloaded_array.axistags,
            laneShape=preloaded_array.shape,
            laneDtype=preloaded_array.dtype,
            **info_kwargs,
        )

    @property
    def effective_path(self) -> str:
        return "Preloaded Array"

    @property
    def legacy_location(self) -> str:
        return "PreloadedArray"

    def to_json_data(self) -> Dict:
        out = super().to_json_data()
        out["preloaded_array"] = self.preloaded_array
        return out

    @property
    def display_string(self) -> str:
        return "Preloaded Array"

    def get_non_transposed_provider_slot(
        self, parent: Optional[Operator] = None, graph: Optional[Graph] = None
    ) -> OutputSlot:
        opReader = OpArrayPiper(parent=parent, graph=graph)
        opReader.Input.setValue(self.preloaded_array)
        return opReader.Output


class DummyDatasetInfo(DatasetInfo):
    """Special DatasetInfo for datasets that can't be found in headless mode"""

    def __init__(self, **info_kwargs):
        super().__init__(**info_kwargs)

    @property
    def legacy_location(self) -> str:
        return "Outer Space"

    @property
    def effective_path(self) -> str:
        return "Outer Space"

    @property
    def display_string(self) -> str:
        return "Dummy Zero Array"

    def to_json_data(self) -> Dict:
        raise NotImplemented("Dummy Slots should not be serialized!")

    def get_non_transposed_provider_slot(
        self, parent: Optional[Operator] = None, graph: Optional[Graph] = None
    ) -> OutputSlot:
        opZero = OpMissingDataSource(
            shape=self.laneShape, dtype=self.laneDtype, axistags=self.axistags, parent=parent, graph=graph
        )
        return opZero.Output

    @classmethod
    def from_h5_group(cls, data: h5py.Group, params: Dict = None):
        laneShape = tuple(data["shape"])
        default_tags = get_default_axisordering(laneShape)

        params = params or {}
        if "laneShape" not in params:
            params["laneShape"] = laneShape
        if "default_tags" not in params:
            params["default_tags"] = default_tags
        if "laneDtype" not in params:
            params["laneDtype"] = numpy.uint8
        return super().from_h5_group(data, params)


class UrlDatasetInfo(DatasetInfo):
    def __init__(self, *, url: str, nickname: str = "", **info_kwargs):
        self.url = url
        op_reader = OpInputDataReader(graph=Graph(), FilePath=self.url)
        meta = op_reader.Output.meta.copy()
        super().__init__(
            default_tags=meta.axistags,
            nickname=nickname or self.url.rstrip("/").split("/")[-1],
            laneShape=meta.shape,
            laneDtype=meta.dtype,
            **info_kwargs,
        )

    @property
    def legacy_location(self) -> str:
        return "FileSystem"

    @property
    def effective_path(self) -> str:
        return self.url

    def get_non_transposed_provider_slot(
        self, parent: Optional[Operator] = None, graph: Optional[Graph] = None
    ) -> OutputSlot:
        op_reader = OpInputDataReader(parent=parent, graph=graph, FilePath=self.url)
        return op_reader.Output

    @property
    def display_string(self):
        return "Remote: " + self.url

    def to_json_data(self) -> Dict:
        out = super().to_json_data()
        out["url"] = self.url
        return out

    @classmethod
    def from_h5_group(cls, group: h5py.Group):
        return super().from_h5_group(group, {"url": group["filePath"][()].decode("utf-8")})


class FilesystemDatasetInfo(DatasetInfo):
    def __init__(
        self,
        *,
        filePath: str,
        project_file: h5py.File = None,
        sequence_axis: str = None,
        nickname: str = "",
        drange: Tuple[Number, Number] = None,
        **info_kwargs,
    ):
        """
        sequence_axis: Axis along which to stack (only applicable for stacks).
        """
        self.sequence_axis = sequence_axis
        self.base_dir = str(Path(project_file.filename).absolute().parent) if project_file else os.getcwd()
        assert os.path.isabs(self.base_dir)  # FIXME: if file_project was opened as a relative path, this would break
        self.expanded_paths = self.expand_path(filePath, cwd=self.base_dir)
        assert len(self.expanded_paths) == 1 or self.sequence_axis
        if len({PathComponents(ep).extension for ep in self.expanded_paths}) > 1:
            raise Exception(f"Multiple extensions unsupported as a single data source: {self.expanded_paths}")
        self.filePath = os.path.pathsep.join(self.expanded_paths)

        op_reader = OpInputDataReader(
            graph=Graph(), WorkingDirectory=self.base_dir, FilePath=self.filePath, SequenceAxis=self.sequence_axis
        )
        meta = op_reader.Output.meta.copy()
        op_reader.cleanUp()

        super().__init__(
            default_tags=meta.axistags,
            nickname=nickname or self.create_nickname(self.expanded_paths),
            laneShape=meta.shape,
            laneDtype=meta.dtype,
            drange=drange or meta.get("drange"),
            **info_kwargs,
        )

    @property
    def legacy_location(self) -> str:
        return "FileSystem"

    @property
    def default_output_dir(self) -> Path:
        first_external_path = PathComponents(self.filePath.split(os.path.pathsep)[0]).externalPath
        return Path(first_external_path).parent

    def get_non_transposed_provider_slot(
        self, parent: Optional[Operator] = None, graph: Optional[Graph] = None
    ) -> OutputSlot:
        op_reader = OpInputDataReader(
            parent=parent,
            graph=graph,
            WorkingDirectory=self.base_dir,
            FilePath=self.filePath,
            SequenceAxis=self.sequence_axis,
        )
        return op_reader.Output

    @classmethod
    def from_h5_group(cls, data: h5py.Group):
        params = {"project_file": data.file, "filePath": data["filePath"][()].decode("utf-8")}
        return super().from_h5_group(data, params)

    def isHdf5(self) -> bool:
        return any(self.pathIsHdf5(ep) for ep in self.external_paths)

    def isNpz(self) -> bool:
        return any(self.pathIsNpz(ep) for ep in self.external_paths)

    def isN5(self) -> bool:
        return any(self.pathIsN5(ep) for ep in self.external_paths)

    def is_hierarchical(self):
        return self.isHdf5() or self.isNpz() or self.isN5()

    def is_in_filesystem(self) -> bool:
        return True

    @property
    def display_string(self):
        return "Absolute Link: " + self.effective_path

    @property
    def effective_path(self) -> str:
        return os.path.pathsep.join(self.expanded_paths)

    def is_under_project_file(self) -> bool:
        try:
            self.get_relative_paths()
            return True
        except CantSaveAsRelativePathsException:
            return False

    def get_relative_paths(self) -> List[str]:
        external_paths = [Path(PathComponents(path).externalPath) for path in self.expanded_paths]
        try:
            return sorted([str(ep.absolute().relative_to(self.base_dir)) for ep in external_paths])
        except ValueError:
            raise CantSaveAsRelativePathsException(self.filePath, self.base_dir)

    @property
    def external_paths(self) -> List[str]:
        return [PathComponents(ep).externalPath for ep in self.expanded_paths]

    @property
    def internal_paths(self) -> List[str]:
        return [PathComponents(ep).internalPath for ep in self.expanded_paths]

    @property
    def file_extensions(self) -> List[str]:
        return [PathComponents(ep).extension for ep in self.expanded_paths]

    def getPossibleInternalPaths(self):
        possible_internal_paths = set()
        for expanded_path in self.expanded_paths:
            external_path = PathComponents(expanded_path).externalPath
            possible_internal_paths |= set(self.getPossibleInternalPathsFor(external_path))
        return possible_internal_paths


class RelativeFilesystemDatasetInfo(FilesystemDatasetInfo):
    def __init__(self, **fs_info_kwargs):
        super().__init__(**fs_info_kwargs)
        if not self.is_under_project_file():
            raise CantSaveAsRelativePathsException(self.filePath, self.base_dir)

    @classmethod
    def create_or_fallback_to_absolute(cls, *args, **kwargs):
        try:
            return cls(*args, **kwargs)
        except CantSaveAsRelativePathsException:
            return FilesystemDatasetInfo(*args, **kwargs)

    @property
    def display_string(self):
        return "Relative Link: " + self.effective_path

    @property
    def effective_path(self) -> str:
        return os.path.pathsep.join(str(Path(p).relative_to(self.base_dir)) for p in self.expanded_paths)


class OpDataSelection(Operator):
    """
    The top-level operator for the data selection applet, implemented as a single-image operator.
    The applet uses an OperatorWrapper to make it suitable for use in a workflow.
    """

    name = "OpDataSelection"
    category = "Top-level"

    SupportedExtensions = OpInputDataReader.SupportedExtensions

    # Inputs
    RoleName = InputSlot(stype="string", value="")
    ProjectFile = InputSlot(stype="object", optional=True)  # : The project hdf5 File object (already opened)
    # : The internal path to the hdf5 group where project-local datasets are stored within the project file
    ProjectDataGroup = InputSlot(stype="string", optional=True)
    WorkingDirectory = InputSlot(stype="filestring")  # : The filesystem directory where the project file is located
    Dataset = InputSlot(stype="object")  # : A DatasetInfo object

    # Outputs
    Image = OutputSlot()  # : The output image
    AllowLabels = OutputSlot(stype="bool")  # : A bool indicating whether or not this image can be used for training

    ImageName = OutputSlot(stype="string")  # : The name of the output image

    def __init__(
        self,
        forceAxisOrder: List[str] = ["tczyx"],
        ProjectFile: h5py.File = None,
        ProjectDataGroup=None,
        WorkingDirectory=None,
        Dataset: DatasetInfo = None,
        *args,
        **kwargs,
    ):
        """
        forceAxisOrder: How to auto-reorder the input data before connecting it to the rest of the workflow.
                        Should be a list of input orders that are allowed by the workflow
                        For example, if the workflow can handle 2D and 3D, you might pass ['yxc', 'zyxc'].
                        If it only handles exactly 5D, you might pass 'tzyxc', assuming that's how you wrote the
                        workflow.
                        todo: move toward 'tczyx' standard.
        """
        super(OpDataSelection, self).__init__(*args, **kwargs)
        self.forceAxisOrder = forceAxisOrder
        self._opReaders = []

        # If the gui calls disconnect() on an input slot without replacing it with something else,
        #  we still need to clean up the internal operator that was providing our data.
        self.ProjectFile.notifyUnready(self.internalCleanup)
        self.ProjectFile.setOrConnectIfAvailable(ProjectFile)

        self.ProjectDataGroup.notifyUnready(self.internalCleanup)
        self.ProjectDataGroup.setOrConnectIfAvailable(ProjectDataGroup)

        self.WorkingDirectory.notifyUnready(self.internalCleanup)
        self.WorkingDirectory.setOrConnectIfAvailable(WorkingDirectory)

        self.Dataset.notifyUnready(self.internalCleanup)
        self.Dataset.setOrConnectIfAvailable(Dataset)

    def internalCleanup(self, *args):
        if len(self._opReaders) > 0:
            self.Image.disconnect()
            for reader in reversed(self._opReaders):
                reader.cleanUp()
            self._opReaders = []

    def setupOutputs(self):
        self.internalCleanup()
        datasetInfo = self.Dataset.value

        try:
            role_name = self.RoleName.value
            if datasetInfo.shape5d.c > 1:
                meta = {"channel_names": [f"{role_name}-{i}" for i in range(datasetInfo.shape5d.c)]}
            else:
                meta = {"channel_names": [role_name]}
            providerSlot = datasetInfo.get_provider_slot(meta=meta, parent=self)
            opReader = providerSlot.operator
            self._opReaders.append(opReader)

            # make sure that x and y axes are present in the selected axis order
            if "x" not in providerSlot.meta.axistags or "y" not in providerSlot.meta.axistags:
                raise DatasetConstraintError(
                    "DataSelection", "Data must always have at leaset the axes x and y for ilastik to work."
                )

            if self.forceAxisOrder:
                assert isinstance(
                    self.forceAxisOrder, list
                ), "forceAxisOrder should be a *list* of preferred axis orders"

                # Before we re-order, make sure no non-singleton
                #  axes would be dropped by the forced order.
                tagged_provider_shape = providerSlot.meta.getTaggedShape()
                minimal_axes = {k for k, v in tagged_provider_shape.items() if v > 1}

                # Pick the shortest of the possible 'forced' orders that
                # still contains all the axes of the original dataset.
                candidate_orders = list(self.forceAxisOrder)
                candidate_orders = [order for order in candidate_orders if minimal_axes.issubset(set(order))]

                if len(candidate_orders) == 0:
                    msg = (
                        f"The axes of your dataset ({providerSlot.meta.getAxisKeys()}) are not compatible with "
                        f"any of the allowed axis configurations used by this workflow ({self.forceAxisOrder})."
                    )
                    raise DatasetConstraintError("DataSelection", msg)

                output_order = sorted(candidate_orders, key=len)[0]  # the shortest one
                output_order = "".join(output_order)
            else:
                # No forced axisorder is supplied. Use original axisorder as
                # output order: it is assumed by the export-applet, that the
                # an OpReorderAxes operator is added in the beginning
                output_order = providerSlot.meta.getAxisKeys()
            if "c" not in output_order:
                output_order += "c"

            op5 = OpReorderAxes(parent=self, AxisOrder=output_order, Input=providerSlot)
            self._opReaders.append(op5)

            self.Image.connect(op5.Output)

            self.AllowLabels.setValue(datasetInfo.allowLabels)

            if self.Image.meta.nickname is not None:
                datasetInfo.nickname = self.Image.meta.nickname

            self.ImageName.setValue(datasetInfo.nickname)

        except:
            self.internalCleanup()
            raise

    def propagateDirty(self, slot, subindex, roi):
        # Output slots are directly connected to internal operators
        pass

    @classmethod
    def getInternalDatasets(cls, filePath):
        return OpInputDataReader.getInternalDatasets(filePath)


class OpDataSelectionGroup(Operator):
    # Inputs
    ProjectFile = InputSlot(stype="object", optional=True)
    ProjectDataGroup = InputSlot(stype="string", optional=True)
    WorkingDirectory = InputSlot(stype="filestring")
    DatasetRoles = InputSlot(stype="object")

    # Must mark as optional because not all subslots are required.
    DatasetGroup = InputSlot(stype="object", level=1, optional=True)

    # Outputs
    ImageGroup = OutputSlot(level=1)

    # These output slots are provided as a convenience, since otherwise it is tricky to create a lane-wise multislot of
    # level-1 for only a single role.
    # (It can be done, but requires OpTransposeSlots to invert the level-2 multislot indexes...)
    Image = OutputSlot()  # The first dataset. Equivalent to ImageGroup[0]
    Image1 = OutputSlot()  # The second dataset. Equivalent to ImageGroup[1]
    Image2 = OutputSlot()  # The third dataset. Equivalent to ImageGroup[2]
    AllowLabels = OutputSlot(stype="bool")  # Pulled from the first dataset only.

    # Must be the LAST slot declared in this class.
    # When the shell detects that this slot has been resized,
    #  it assumes all the others have already been resized.
    ImageName = OutputSlot()  # Name of the first dataset is used.  Other names are ignored.

    def __init__(self, forceAxisOrder=None, *args, **kwargs):
        super(OpDataSelectionGroup, self).__init__(*args, **kwargs)
        self._opDatasets = None
        self._roles = []
        self._forceAxisOrder = forceAxisOrder

        def handleNewRoles(*args):
            self.DatasetGroup.resize(len(self.DatasetRoles.value))

        self.DatasetRoles.notifyReady(handleNewRoles)

    @property
    def role_names(self) -> List[str]:
        return self.DatasetRoles.value

    def get_role_info_slot(self, role: Union[str, int]) -> InputSlot:
        role_index = role if isinstance(role, int) else self.role_names.index(role)
        return self.DatasetGroup[role_index]

    def get_dataset_info(self, role: Union[str, int]) -> Optional[DatasetInfo]:
        slot = self.get_role_info_slot(role)
        if not slot.ready():
            return None
        return slot.value

    def get_infos(self) -> Dict[str, Optional[DatasetInfo]]:
        return {role_name: self.get_dataset_info(role_name) for role_name in self.role_names}

    def get_axistags(self) -> Dict[str, Optional[AxisTags]]:
        return {role_name: info and info.axistags for role_name, info in self.get_infos().items()}

    def configure(self, infos: Dict[str, DatasetInfo]):
        for role_index, role_name in enumerate(self.role_names):
            if role_name in infos:
                self.DatasetGroup[role_index].setValue(infos[role_name])

    def setupOutputs(self):
        # Create internal operators
        if self.DatasetRoles.value != self._roles:
            self._roles = self.DatasetRoles.value
            # Clean up the old operators
            self.ImageGroup.disconnect()
            self.Image.disconnect()
            self.Image1.disconnect()
            self.Image2.disconnect()
            if self._opDatasets is not None:
                self._opDatasets.cleanUp()

            self._opDatasets = OperatorWrapper(
                OpDataSelection,
                parent=self,
                operator_kwargs={"forceAxisOrder": self._forceAxisOrder},
                broadcastingSlotNames=["ProjectFile", "ProjectDataGroup", "WorkingDirectory"],
            )
            self.ImageGroup.connect(self._opDatasets.Image)
            self._opDatasets.Dataset.connect(self.DatasetGroup)
            self._opDatasets.ProjectFile.connect(self.ProjectFile)
            self._opDatasets.ProjectDataGroup.connect(self.ProjectDataGroup)
            self._opDatasets.WorkingDirectory.connect(self.WorkingDirectory)

        for role_index, opDataSelection in enumerate(self._opDatasets):
            opDataSelection.RoleName.setValue(self._roles[role_index])

        if len(self._opDatasets.Image) > 0:
            self.Image.connect(self._opDatasets.Image[0])

            if len(self._opDatasets.Image) >= 2:
                self.Image1.connect(self._opDatasets.Image[1])
            else:
                self.Image1.disconnect()
                self.Image1.meta.NOTREADY = True

            if len(self._opDatasets.Image) >= 3:
                self.Image2.connect(self._opDatasets.Image[2])
            else:
                self.Image2.disconnect()
                self.Image2.meta.NOTREADY = True

            self.ImageName.connect(self._opDatasets.ImageName[0])
            self.AllowLabels.connect(self._opDatasets.AllowLabels[0])
        else:
            self.Image.disconnect()
            self.Image1.disconnect()
            self.Image2.disconnect()
            self.ImageName.disconnect()
            self.AllowLabels.disconnect()
            self.Image.meta.NOTREADY = True
            self.Image1.meta.NOTREADY = True
            self.Image2.meta.NOTREADY = True
            self.ImageName.meta.NOTREADY = True
            self.AllowLabels.meta.NOTREADY = True

    def execute(self, slot, subindex, rroi, result):
        assert False, "Unknown or unconnected output slot: {}".format(slot.name)

    def propagateDirty(self, slot, subindex, roi):
        # Output slots are directly connected to internal operators
        pass


class OpMultiLaneDataSelectionGroup(OpMultiLaneWrapper):
    # TODO: Provide output slots DatasetsByRole and ImagesByRole as a convenience
    #       to save clients the trouble of instantiating/using OpTransposeSlots.
    def __init__(self, forceAxisOrder=False, *args, **kwargs):
        kwargs.update(
            {
                "operator_kwargs": {"forceAxisOrder": forceAxisOrder},
                "broadcastingSlotNames": ["ProjectFile", "ProjectDataGroup", "WorkingDirectory", "DatasetRoles"],
            }
        )
        super(OpMultiLaneDataSelectionGroup, self).__init__(OpDataSelectionGroup, *args, **kwargs)

        # 'value' slots
        assert self.ProjectFile.level == 0
        assert self.ProjectDataGroup.level == 0
        assert self.WorkingDirectory.level == 0
        assert self.DatasetRoles.level == 0

        # Indexed by [lane][role]
        assert self.DatasetGroup.level == 2, "DatasetGroup is supposed to be a level-2 slot, indexed by [lane][role]"

    def addLane(self, laneIndex) -> OpDataSelectionGroup:
        """Reimplemented from base class."""
        numLanes = len(self.innerOperators)

        # Only add this lane if we don't already have it
        # We might be called from within the context of our own insertSlot signal.
        if numLanes == laneIndex:
            super(OpMultiLaneDataSelectionGroup, self).addLane(laneIndex)
        return self.get_lane(laneIndex)

    def removeLane(self, laneIndex, finalLength):
        """Reimplemented from base class."""
        numLanes = len(self.innerOperators)
        if numLanes > finalLength:
            super(OpMultiLaneDataSelectionGroup, self).removeLane(laneIndex, finalLength)

    @property
    def workflow(self) -> Workflow:
        return self.parent

    @property
    def role_names(self) -> List[str]:
        return self.DatasetRoles.value

    def pushLane(self, role_infos: Dict[str, DatasetInfo]):
        original_num_lanes = self.num_lanes
        try:
            lane = self.addLane(self.num_lanes)
            lane.configure(infos=role_infos)
            self.workflow.handleNewLanesAdded()
        except Exception as e:
            self.removeLane(original_num_lanes, original_num_lanes)
            raise e

    def dropLastLane(self):
        self.removeLane(self.num_lanes - 1, self.num_lanes - 1)

    @property
    def num_lanes(self) -> int:
        return len(self.innerOperators)

    def get_lane(self, lane_idx: int) -> OpDataSelectionGroup:
        return self.innerOperators[lane_idx]
