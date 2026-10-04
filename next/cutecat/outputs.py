"""Bounded, root-contained output allocation. Never overwrites existing media."""
import os
from pathlib import PurePosixPath
from .pathsafe import resolve_request, validate_job_paths, assert_writable, normalise_relative, PathSafetyError


def pending_paths(store, roots, exclude=None):
    configured = {root.id: root for root in roots}
    paths = set()
    for root_id, relative in store.pending_outputs(exclude):
        if root_id in configured:
            paths.add(os.path.realpath(os.path.join(configured[root_id].path, normalise_relative(relative))))
    return paths


class OutputConflict(ValueError):
    pass


def allocate_output(roots, source, root_id, relative, reserved, policy="reject", *, skip_original=False):
    original = resolve_request(roots, root_id, relative)
    assert_writable(original)
    if not original.relative:
        raise PathSafetyError("output filename is required")
    # Directory names are collisions in rename mode, but a source alias must
    # still be rejected before searching for a different destination.
    if not original.is_dir:
        validate_job_paths(source, original)
    elif policy != "rename":
        raise OutputConflict("output already exists")
    path = PurePosixPath(original.relative)
    # A dangling symlink is occupied too, even though realpath points elsewhere.
    def occupied(target):
        lexical = os.path.join(target.root.path, target.relative)
        return os.path.lexists(lexical) or os.path.lexists(target.absolute) or str(target.absolute) in reserved
    for index in range(1000):
        name = path.name if index == 0 else f"{path.stem} ({index}){path.suffix}"
        if len(os.fsencode(name)) > 255:
            raise PathSafetyError("output filename is too long")
        target = resolve_request(roots, root_id, str(path.with_name(name)))
        if target.is_dir:
            if policy == "rename":
                continue
            raise OutputConflict("output already exists")
        validate_job_paths(source, target)
        if not (skip_original and index == 0) and not occupied(target):
            return target
        if policy != "rename":
            raise OutputConflict("output already exists or is reserved by another task")
    raise OutputConflict("no available output name after 1000 attempts")
