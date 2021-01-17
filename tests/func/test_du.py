import os
import pytest
import shutil
import textwrap
from operator import itemgetter

from dvc.exceptions import PathMissingError
from dvc.repo import Repo
from dvc.scm.base import CloneError

FS_STRUCTURE = {
    "README.md": "content",
    "model/script.py": "content",
    "model/train.py": "content",
    ".gitignore": "content",
}

DVC_STRUCTURE = {
    "structure.xml": "content",
    "data/subcontent/data.xml": "content",
    "data/subcontent/statistics/data.csv": "content",
    "model/people.csv": "content",
}


def test_du_repo_file(tmp_dir, dvc, scm, mocker, monkeypatch, caplog):
    import logging

    from dvc.cli import parse_args

    tmp_dir.scm_gen(FS_STRUCTURE, commit="init")
    tmp_dir.dvc_gen(DVC_STRUCTURE, commit="dvc")

    #cli_args = parse_args(["du", os.fspath(tmp_dir)])
    cli_args = parse_args(["du"])
    cmd = cli_args.func(cli_args)

    caplog.clear()
    mocker.patch("sys.stdout.isatty", return_value=True)
    with caplog.at_level(logging.INFO, logger="dvc.command.du"):
        assert cmd.run() == 0

    assert caplog.records[-1].msg == "\n".join(
        [
            "du stab works"
        ]
    )
