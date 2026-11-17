import shutil
from argparse import Namespace
from pathlib import Path

from vp import cli

PROFILES = Path(__file__).parent.parent / "profiles"


def test_validate_all_skips_empty_profile_dirs(tmp_path, capsys):
    (tmp_path / "profiles").mkdir()
    shutil.copytree(PROFILES / "brother-dcp-l1630w", tmp_path / "profiles" / "brother-dcp-l1630w")
    (tmp_path / "profiles" / "empty").mkdir()

    rc = cli.cmd_validate(Namespace(profiles=[]), tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    assert "brother-dcp-l1630w" in out
    assert "empty" not in out
