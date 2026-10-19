"""
geper/api/vcf_paths.py
──────────────────────
Where a submitted VCF may live, and who may name it.

`POST /interpretations` takes a `vcf_path`: a path on the SERVER's filesystem. The worker later hashes that file, hands it
to the engine and records the path against the clinical record. A caller holding only an API key must not be able to
point any of that at an arbitrary file (`/etc/passwd.vcf`, another organisation's data, a file behind a symlink), so every
path is checked here, at the API and again in the worker just before the file is touched (a stored row may predate the
policy, and a symlink can change between the two).

THE RULE. There is one approved root, `$GEPER_VCF_ROOT`, and each organisation owns one directory in it:

    $GEPER_VCF_ROOT/<organisation id>/...

A submitted path is accepted only if, after every symlink is resolved, it lies inside that organisation's directory, names
a `.vcf` or `.vcf.gz` file, and contains no `..` component. A relative path is read relative to the organisation's
directory, so a client does not need to know where the root is. The organisation is the one the API key is bound to,
never a field of the request.

A path that is inside the directory but does not exist is ACCEPTED here: the worker already turns a missing file into the
`vcf_missing` clinical exception an operator can act on. An unconfigured root fails CLOSED: nothing is accepted.

WHAT THIS DOES NOT DEFEND. A hard link inside an organisation's directory to another file looks like any other file (a
hard link has no path to resolve); keep the approved root on storage that only the platform writes to. And the check
happens before the file is opened, so a file replaced between the check and the open is the filesystem's race; the worker
re-resolves immediately before use, which narrows the window and does not close it.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional, Union

VCF_ROOT_ENV = "GEPER_VCF_ROOT"
ALLOWED_SUFFIXES = (".vcf", ".vcf.gz")
MAX_PATH_LENGTH = 4096

# What a caller is told. Deliberately not the path, the root, or whether anything exists outside the approved directory.
_PUBLIC_MESSAGES = {
    "vcf_root_not_configured": "VCF storage is not configured on this server.",
    "vcf_path_invalid": "vcf_path is not an approved VCF location for this organisation.",
    "vcf_path_traversal": "vcf_path is not an approved VCF location for this organisation.",
    "vcf_path_outside_organisation_directory": "vcf_path is not an approved VCF location for this organisation.",
    "vcf_path_not_a_vcf": "vcf_path must name a .vcf or .vcf.gz file.",
    "vcf_path_not_a_file": "vcf_path does not name a VCF file.",
}


class VcfPathError(Exception):
    """A submitted VCF path the policy refuses. `code` is stable; `public_message` is safe to return to the caller."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.public_message = _PUBLIC_MESSAGES[code]


def vcf_root_from_env() -> Path:
    """The approved root, resolved. Fails closed: unset, blank, or not an existing directory is an error."""
    configured = (os.environ.get(VCF_ROOT_ENV) or "").strip()
    if not configured:
        raise VcfPathError("vcf_root_not_configured", f"{VCF_ROOT_ENV} is not set")
    root = Path(configured).resolve(strict=False)
    if not root.is_dir():
        raise VcfPathError("vcf_root_not_configured", f"{VCF_ROOT_ENV} does not name a directory")
    return root


def organisation_directory(org_id: Union[str, uuid.UUID], root: Optional[Path] = None) -> Path:
    """The (resolved) directory the organisation's VCFs must live in."""
    root = root if root is not None else vcf_root_from_env()
    return (root / str(org_id)).resolve(strict=False)


def _components(raw: str) -> list:
    return [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")]


def resolve_vcf_path(raw: object, org_id: Union[str, uuid.UUID], root: Optional[Path] = None) -> Path:
    """
    The canonical path of a VCF this organisation may use, or `VcfPathError`. See the module docstring for the rule.
    Nothing is created, opened or read; only symlinks on the way are resolved.
    """
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw or len(raw) > MAX_PATH_LENGTH:
        raise VcfPathError("vcf_path_invalid")
    if ".." in _components(raw):
        raise VcfPathError("vcf_path_traversal")
    if os.name == "nt":
        # an alternate data stream or a device name rides on a colon after the drive letter
        rest = raw[2:] if len(raw) >= 2 and raw[1] == ":" else raw
        if ":" in rest:
            raise VcfPathError("vcf_path_invalid")

    org_dir = organisation_directory(org_id, root)
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = org_dir / candidate
    try:
        resolved = candidate.resolve(strict=False)  # follows every existing symlink on the way
    except (OSError, RuntimeError, ValueError):
        raise VcfPathError("vcf_path_invalid") from None
    if resolved == org_dir or not resolved.is_relative_to(org_dir):
        raise VcfPathError("vcf_path_outside_organisation_directory")
    if not resolved.name.lower().endswith(ALLOWED_SUFFIXES):
        raise VcfPathError("vcf_path_not_a_vcf")
    if resolved.exists() and not resolved.is_file():
        raise VcfPathError("vcf_path_not_a_file")
    return resolved
