"""
api/tests/test_vcf_paths.py
───────────────────────────
No database. The policy for a submitted `vcf_path` (geper/api/vcf_paths.py): inside the organisation's own directory under
the one approved root, after every symlink is resolved, a .vcf or .vcf.gz file, no `..`.

Symlink and junction escapes are exercised for real where the platform lets this process make them (a POSIX symlink; on
Windows a directory junction, which needs no privilege, and a file symlink when allowed). A case that cannot be built here
is skipped with the reason, never passed vacuously.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from api import vcf_paths
from api.vcf_paths import VcfPathError, resolve_vcf_path, vcf_root_from_env

ORG = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_ORG = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
VCF = "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\n"


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "approved-root"
    (root / str(ORG)).mkdir(parents=True)
    (root / str(OTHER_ORG)).mkdir()
    monkeypatch.setenv("GEPER_VCF_ROOT", str(root))
    return root


@pytest.fixture
def org_dir(root):
    return (root / str(ORG)).resolve()


def make_vcf(directory: Path, name="sample.vcf") -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(VCF, encoding="utf-8")
    return path


def refused(raw, code, org=ORG):
    with pytest.raises(VcfPathError) as caught:
        resolve_vcf_path(raw, org)
    assert caught.value.code == code, (raw, caught.value.code)
    return caught.value


# ═══ what is accepted ═══


def test_an_absolute_path_inside_the_organisations_directory_is_accepted(org_dir):
    path = make_vcf(org_dir)
    assert resolve_vcf_path(str(path), ORG) == path.resolve()


@pytest.mark.parametrize("name", ["sample.vcf", "sample.vcf.gz", "SAMPLE.VCF", "run.2026.Vcf.GZ", "dir/sub/x.vcf"])
def test_a_relative_path_is_read_relative_to_the_organisations_directory(org_dir, name):
    assert resolve_vcf_path(name, ORG) == (org_dir / name).resolve()


def test_a_path_inside_the_directory_that_does_not_exist_yet_is_accepted_for_the_worker_to_report(org_dir):
    """The worker turns a missing file into the `vcf_missing` exception an operator can act on."""
    assert resolve_vcf_path(str(org_dir / "not-there-yet.vcf"), ORG) == (org_dir / "not-there-yet.vcf").resolve()


def test_the_organisation_may_be_given_as_a_string_or_a_uuid(org_dir):
    assert resolve_vcf_path("a.vcf", str(ORG)) == resolve_vcf_path("a.vcf", ORG)


def test_a_symlink_that_stays_inside_the_directory_is_accepted(org_dir):
    target = make_vcf(org_dir, "real.vcf")
    link = org_dir / "alias.vcf"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a file symlink here ({exc})")
    assert resolve_vcf_path(str(link), ORG) == target.resolve()


# ═══ what is refused ═══


def test_an_arbitrary_absolute_path_is_refused(tmp_path, root):
    outside = make_vcf(tmp_path / "elsewhere", "secrets.vcf")
    refused(str(outside), "vcf_path_outside_organisation_directory")
    refused("/etc/passwd.vcf", "vcf_path_outside_organisation_directory")


def test_another_organisations_directory_is_refused(root):
    theirs = make_vcf(root / str(OTHER_ORG))
    refused(str(theirs), "vcf_path_outside_organisation_directory")


def test_the_approved_root_itself_and_the_organisation_directory_are_refused(root):
    refused(str(root / "x.vcf"), "vcf_path_outside_organisation_directory")
    refused(str(root / str(ORG)), "vcf_path_outside_organisation_directory")


def test_a_sibling_whose_name_merely_starts_with_the_organisation_id_is_refused(root):
    sibling = root / f"{ORG}-evil"
    make_vcf(sibling)
    refused(str(sibling / "sample.vcf"), "vcf_path_outside_organisation_directory")


@pytest.mark.parametrize(
    "raw",
    [
        "../x.vcf",
        "..\\x.vcf",
        "sub/../../x.vcf",
        "sub\\..\\..\\x.vcf",
        "a/b/../../../x.vcf",
        "..",
        "./../x.vcf",
    ],
)
def test_traversal_is_refused(org_dir, raw):
    refused(raw, "vcf_path_traversal")


def test_a_dot_dot_component_is_refused_even_when_it_stays_inside(org_dir):
    """Not only the ones that escape: a path that needs `..` to be read is not one anybody should be sending."""
    refused(str(org_dir / "a" / ".." / "x.vcf"), "vcf_path_traversal")
    refused("a/../x.vcf", "vcf_path_traversal")


@pytest.mark.parametrize("raw", [None, 5, 3.5, ["a.vcf"], b"a.vcf", "", "   ", "a\x00b.vcf", "x" * 5000 + ".vcf"])
def test_a_value_that_is_not_a_usable_path_is_refused(org_dir, raw):
    refused(raw, "vcf_path_invalid")


@pytest.mark.parametrize("name", ["x.txt", "x.vcf.txt", "x", "x.vcf.bak", "x.bcf", "x.vcfx", "x.gz", "vcf"])
def test_only_vcf_and_vcf_gz_files_are_accepted(org_dir, name):
    refused(name, "vcf_path_not_a_vcf")


def test_a_directory_named_like_a_vcf_is_refused(org_dir):
    (org_dir / "looks-like.vcf").mkdir()
    refused("looks-like.vcf", "vcf_path_not_a_file")


# ═══ symlink and junction escapes ═══


def _link_file(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a file symlink here ({exc})")


def _link_directory(link: Path, target: Path) -> None:
    """A directory symlink, or on Windows a junction (no privilege needed); both are followed by path resolution."""
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip(f"cannot create a junction here ({result.stderr.strip() or result.stdout.strip()})")
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a directory symlink here ({exc})")


def test_a_file_symlink_that_leaves_the_directory_is_refused(tmp_path, org_dir):
    outside = make_vcf(tmp_path / "elsewhere", "private.vcf")
    link = org_dir / "innocent.vcf"
    _link_file(link, outside)
    refused(str(link), "vcf_path_outside_organisation_directory")
    refused("innocent.vcf", "vcf_path_outside_organisation_directory")


def test_a_file_symlink_to_another_organisations_file_is_refused(root, org_dir):
    theirs = make_vcf(root / str(OTHER_ORG), "theirs.vcf")
    link = org_dir / "borrowed.vcf"
    _link_file(link, theirs)
    refused("borrowed.vcf", "vcf_path_outside_organisation_directory")


def test_a_directory_link_that_leaves_the_directory_is_refused(tmp_path, org_dir):
    outside_dir = tmp_path / "elsewhere-dir"
    make_vcf(outside_dir, "x.vcf")
    _link_directory(org_dir / "shared", outside_dir)
    refused("shared/x.vcf", "vcf_path_outside_organisation_directory")
    refused(str(org_dir / "shared" / "x.vcf"), "vcf_path_outside_organisation_directory")


def test_a_directory_link_to_another_organisation_is_refused(root, org_dir):
    make_vcf(root / str(OTHER_ORG), "x.vcf")
    _link_directory(org_dir / "neighbour", root / str(OTHER_ORG))
    refused("neighbour/x.vcf", "vcf_path_outside_organisation_directory")


def test_a_link_chain_that_ends_outside_is_refused(tmp_path, org_dir):
    outside = make_vcf(tmp_path / "elsewhere", "end.vcf")
    _link_file(org_dir / "second.vcf", outside)
    _link_file(org_dir / "first.vcf", org_dir / "second.vcf")
    refused("first.vcf", "vcf_path_outside_organisation_directory")


def test_a_symlinked_approved_root_is_followed_once_and_still_enforced(tmp_path, monkeypatch):
    real = tmp_path / "real-root"
    (real / str(ORG)).mkdir(parents=True)
    alias = tmp_path / "alias-root"
    _link_directory(alias, real)
    monkeypatch.setenv("GEPER_VCF_ROOT", str(alias))
    path = make_vcf(real / str(ORG))
    assert resolve_vcf_path(str(path), ORG) == path.resolve()
    assert resolve_vcf_path(str(alias / str(ORG) / "sample.vcf"), ORG) == path.resolve()
    outside = make_vcf(tmp_path / "elsewhere")
    refused(str(outside), "vcf_path_outside_organisation_directory")


# ═══ configuration fails closed ═══


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_unconfigured_root_refuses_everything(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("GEPER_VCF_ROOT", raising=False)
    else:
        monkeypatch.setenv("GEPER_VCF_ROOT", value)
    with pytest.raises(VcfPathError) as caught:
        resolve_vcf_path("a.vcf", ORG)
    assert caught.value.code == "vcf_root_not_configured"
    with pytest.raises(VcfPathError):
        vcf_root_from_env()


def test_a_root_that_is_not_a_directory_refuses_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("GEPER_VCF_ROOT", str(tmp_path / "no-such-root"))
    with pytest.raises(VcfPathError) as caught:
        resolve_vcf_path("a.vcf", ORG)
    assert caught.value.code == "vcf_root_not_configured"


# ═══ what a caller is told ═══


def test_what_a_caller_is_told_names_neither_the_path_nor_the_root(tmp_path, root):
    outside = make_vcf(tmp_path / "elsewhere", "secret-name.vcf")
    for raw in (str(outside), "../secret-name.vcf", "/etc/passwd.vcf"):
        with pytest.raises(VcfPathError) as caught:
            resolve_vcf_path(raw, ORG)
        message = caught.value.public_message
        assert str(root) not in message and "secret-name" not in message and "passwd" not in message
    # the outside spellings that exist and those that do not are indistinguishable to the caller
    existing = refused(str(outside), "vcf_path_outside_organisation_directory").public_message
    missing = refused(str(tmp_path / "nowhere" / "x.vcf"), "vcf_path_outside_organisation_directory").public_message
    assert existing == missing


@pytest.mark.skipif(os.name != "nt", reason="alternate data streams and device names are a Windows path feature")
@pytest.mark.parametrize("name", ["x.vcf:stream", "x.vcf::$DATA", "CON:x.vcf"])
def test_a_colon_after_the_drive_is_refused_on_windows(org_dir, name):
    refused(name, "vcf_path_invalid")


def test_the_module_documents_the_environment_variable():
    assert vcf_paths.VCF_ROOT_ENV == "GEPER_VCF_ROOT" and vcf_paths.ALLOWED_SUFFIXES == (".vcf", ".vcf.gz")
