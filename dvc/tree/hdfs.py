import io
import logging
import os
import re
import shutil
import subprocess
from collections import deque
from contextlib import closing, contextmanager
from urllib.parse import urlparse

from dvc.hash_info import HashInfo
from dvc.progress import Tqdm
from dvc.scheme import Schemes
from dvc.utils import fix_env, tmp_fname

from .base import BaseTree, RemoteCmdError
from .pool import get_connection

logger = logging.getLogger(__name__)


class HDFSTree(BaseTree):
    scheme = Schemes.HDFS
    REQUIRES = {"pyarrow": "pyarrow"}
    REGEX = r"^hdfs://((?P<user>.*)@)?.*$"
    PARAM_CHECKSUM = "checksum"
    TRAVERSE_PREFIX_LEN = 2

    def __init__(self, repo, config):
        super().__init__(repo, config)

        self.path_info = None
        url = config.get("url")
        if not url:
            return

        parsed = urlparse(url)
        user = parsed.username or config.get("user")

        self.path_info = self.PATH_CLS.from_parts(
            scheme=self.scheme,
            host=parsed.hostname,
            user=user,
            port=parsed.port,
            path=parsed.path,
        )

    @staticmethod
    def hdfs(path_info):
        import pyarrow.fs

        # NOTE: HadoopFileSystem is not meant to be closed by us and doesn't
        # have close() method or alternative, so we add a noop one to satisfy
        # our connection pool.
        class HDFSConnection(pyarrow.fs.HadoopFileSystem):
            def close(self):
                pass

        return get_connection(
            HDFSConnection,
            path_info.host,
            path_info.port,
            user=path_info.user,
        )

    @contextmanager
    def open(self, path_info, mode="r", encoding=None):
        assert mode in {"r", "rt", "rb"}

        try:
            with self.hdfs(path_info) as hdfs, closing(
                hdfs.open_input_stream(path_info.path)
            ) as fd:
                if mode == "rb":
                    yield fd
                else:
                    yield io.TextIOWrapper(fd, encoding=encoding)
        except OSError as e:
            # Empty .errno and not specific enough error class in pyarrow,
            # see https://issues.apache.org/jira/browse/ARROW-6248
            if "file does not exist" in str(e):
                raise FileNotFoundError(*e.args)
            raise

    def exists(self, path_info, use_dvcignore=True):
        assert not isinstance(path_info, list)
        assert path_info.scheme == "hdfs"
        with self.hdfs(path_info) as hdfs:
            import pyarrow.fs

            file_info = hdfs.get_file_info(path_info.path)
            return file_info.type != pyarrow.fs.FileType.NotFound

    def _walk(self, hdfs, root, topdown=True):
        import posixpath

        from pyarrow.fs import FileSelector, FileType

        dirs = deque()
        nondirs = deque()

        selector = FileSelector(root)
        for entry in hdfs.get_file_info(selector):
            if entry.type == FileType.Directory:
                dirs.append(entry.base_name)
            else:
                nondirs.append(entry.base_name)

        if topdown:
            yield root, dirs, nondirs

        for dname in dirs:
            # NOTE: posixpath.join() is slower
            yield from self._walk(
                hdfs, f"{root}{posixpath.sep}{dname}", topdown=topdown
            )

        if not topdown:
            yield root, dirs, nondirs

    def walk(self, path_info, **kwargs):
        if not self.isdir(path_info):
            return

        with self.hdfs(path_info) as hdfs:
            for root, dnames, fnames in self._walk(
                hdfs, path_info.path, **kwargs
            ):
                yield path_info.replace(path=root), dnames, fnames

    def walk_files(self, path_info, **kwargs):
        for root, _, fnames in self.walk(path_info):
            yield from (root / fname for fname in fnames)

    def remove(self, path_info):
        if path_info.scheme != "hdfs":
            raise NotImplementedError

        if self.exists(path_info):
            logger.debug(f"Removing {path_info.path}")
            with self.hdfs(path_info) as hdfs:
                import pyarrow.fs

                file_info = hdfs.get_file_info(path_info.path)
                if file_info.type == pyarrow.fs.FileType.Directory:
                    hdfs.delete_dir(path_info.path)
                else:
                    hdfs.delete_file(path_info.path)

    def makedirs(self, path_info):
        with self.hdfs(path_info) as hdfs:
            # NOTE: fs.create_dir creates parents by default
            hdfs.create_dir(path_info.path)

    def copy(self, from_info, to_info, **_kwargs):
        # NOTE: hdfs.copy_file is not supported yet in pyarrow
        with self.hdfs(to_info) as hdfs:
            # NOTE: this is how `hadoop fs -cp` works too: it copies through
            # your local machine.
            with closing(hdfs.open_input_stream(from_info.path)) as from_fobj:
                tmp_info = to_info.parent / tmp_fname(to_info.name)
                try:
                    with closing(
                        hdfs.open_output_stream(tmp_info.path)
                    ) as tmp_fobj:
                        shutil.copyfileobj(from_fobj, tmp_fobj)
                    hdfs.move(tmp_info.path, to_info.path)
                except Exception:
                    self.remove(tmp_info)
                    raise

    def isfile(self, path_info):
        with self.hdfs(path_info) as hdfs:
            import pyarrow.fs

            file_info = hdfs.get_file_info(path_info.path)
            return file_info.type == pyarrow.fs.FileType.File

    def isdir(self, path_info):
        with self.hdfs(path_info) as hdfs:
            import pyarrow.fs

            file_info = hdfs.get_file_info(path_info.path)
            return file_info.type == pyarrow.fs.FileType.Directory

    def getsize(self, path_info):
        with self.hdfs(path_info) as hdfs:
            file_info = hdfs.get_file_info(path_info.path)
            return file_info.size

    def hadoop_fs(self, cmd, user=None):
        cmd = "hadoop fs -" + cmd
        if user:
            cmd = f"HADOOP_USER_NAME={user} " + cmd

        # NOTE: close_fds doesn't work with redirected stdin/stdout/stderr.
        # See https://github.com/iterative/dvc/issues/1197.
        close_fds = os.name != "nt"

        executable = os.getenv("SHELL") if os.name != "nt" else None
        p = subprocess.Popen(
            cmd,
            shell=True,
            close_fds=close_fds,
            executable=executable,
            env=fix_env(os.environ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = p.communicate()
        if p.returncode != 0:
            raise RemoteCmdError(self.scheme, cmd, p.returncode, err)
        return out.decode("utf-8")

    @staticmethod
    def _group(regex, s, gname):
        match = re.match(regex, s)
        assert match is not None
        return match.group(gname)

    def get_file_hash(self, path_info):
        # NOTE: pyarrow doesn't support checksum, so we need to use hadoop
        regex = r".*\t.*\t(?P<checksum>.*)"
        stdout = self.hadoop_fs(
            f"checksum {path_info.url}", user=path_info.user
        )
        hash_info = HashInfo(
            self.PARAM_CHECKSUM, self._group(regex, stdout, "checksum"),
        )

        with self.hdfs(path_info) as hdfs:
            file_info = hdfs.get_file_info(path_info.path)
            hash_info.size = file_info.size

        return hash_info

    def _upload(
        self, from_file, to_info, name=None, no_progress_bar=False, **_kwargs
    ):
        with self.hdfs(to_info) as hdfs:
            tmp_file = tmp_fname(to_info.path)
            total = os.path.getsize(from_file)
            with open(from_file, "rb") as fobj:
                with Tqdm.wrapattr(
                    fobj,
                    "read",
                    desc=name,
                    total=total,
                    disable=no_progress_bar,
                ) as wrapped:
                    with hdfs.open_output_stream(tmp_file) as sobj:
                        sobj.write(wrapped.read())
            hdfs.move(tmp_file, to_info.path)

    def _download(
        self, from_info, to_file, name=None, no_progress_bar=False, **_kwargs
    ):
        with self.hdfs(from_info) as hdfs:
            file_info = hdfs.get_file_info(from_info.path)
            total = file_info.size
            with open(to_file, "wb+") as fobj:
                with Tqdm.wrapattr(
                    fobj,
                    "write",
                    desc=name,
                    total=total,
                    disable=no_progress_bar,
                ) as wrapped:
                    with hdfs.open_input_stream(from_info.path) as sobj:
                        wrapped.write(sobj.read())
