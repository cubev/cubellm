from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Final, Protocol, cast

import pytest


class _CommandRunner(Protocol):
    def __call__(
        self,
        command: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]: ...


class _VerifierModule(Protocol):
    main: Callable[
        [
            Sequence[str] | None,
            Mapping[str, str] | None,
            Callable[[Path], ModuleType | None],
            _CommandRunner,
        ],
        int,
    ]


_REPO_ROOT: Final = Path(__file__).resolve().parents[3]
_MODULE_PATH: Final = _REPO_ROOT / ".github" / "scripts" / "verify_linux_native_wheel.py"
_SPEC: Final = importlib.util.spec_from_file_location("verify_linux_native_wheel", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_LOADED_VERIFIER: Final = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _LOADED_VERIFIER
_SPEC.loader.exec_module(_LOADED_VERIFIER)
verifier: Final = cast(_VerifierModule, _LOADED_VERIFIER)

_EXPECTED_TAG: Final = "cp310-abi3-linux_x86_64"
_NATIVE_MEMBER: Final = "litellm/rust_bridge/_native.abi3.so"
_DIST_INFO: Final = "litellm-1.100.0.dist-info"


def _write_wheel(
    tmp_path: Path,
    *,
    filename_tag: str,
    metadata_tags: tuple[str, ...] | None = (_EXPECTED_TAG,),
    dist_info: str = _DIST_INFO,
    duplicate_wheel: bool = False,
    native_bytes: bytes = b"synthetic native extension",
) -> Path:
    wheel: Final = tmp_path / f"litellm-1.100.0-{filename_tag}.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_NATIVE_MEMBER, native_bytes)
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\nName: litellm\nVersion: 1.100.0\n",
        )
        archive.writestr(
            f"{dist_info}/RECORD",
            f"{_NATIVE_MEMBER},,\n{dist_info}/WHEEL,,\n",
        )
        if metadata_tags is not None:
            wheel_metadata: Final = (
                "Wheel-Version: 1.0\nGenerator: regression-test\nRoot-Is-Purelib: false\n"
                + "".join(f"Tag: {tag}\n" for tag in metadata_tags)
            )
            archive.writestr(f"{dist_info}/WHEEL", wheel_metadata)
            if duplicate_wheel:
                archive.writestr(f"{dist_info}/WHEEL", wheel_metadata)
    return wheel


def _fake_subprocess_run(
    command: tuple[str, ...],
    *,
    check: bool,
    capture_output: bool,
    text: bool,
) -> subprocess.CompletedProcess[str]:
    assert check and capture_output and text
    if command == ("rustc", "--version"):
        return subprocess.CompletedProcess(command, 0, stdout="rustc 1.98.0 (regression-test)\n", stderr="")
    if "--sections" in command:
        return subprocess.CompletedProcess(command, 0, stdout="[ 1] .text PROGBITS\n", stderr="")
    if "--dyn-syms" in command:
        return subprocess.CompletedProcess(command, 0, stdout="PyInit__native\n", stderr="")
    raise AssertionError(f"unexpected subprocess command: {command}")


class _NativeModuleWithPanicHook(ModuleType):
    def _panic_for_test(self) -> None:
        return None


def _run_verifier(
    wheel: Path,
    *,
    exposes_panic: bool = False,
    glibc_versions: str = "GLIBC_2.17",
) -> int:
    native_module: Final = (
        _NativeModuleWithPanicHook("litellm.rust_bridge._native")
        if exposes_panic
        else ModuleType("litellm.rust_bridge._native")
    )

    def _fake_load_native_module(_: Path) -> ModuleType:
        return native_module

    def run_command(
        command: tuple[str, ...],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        if "--version-info" in command:
            return subprocess.CompletedProcess(command, 0, stdout=glibc_versions, stderr="")
        return _fake_subprocess_run(command, check=check, capture_output=capture_output, text=text)

    environment: Final = MappingProxyType({"GITHUB_STEP_SUMMARY": str(wheel.parent / "summary.md")})
    return verifier.main(
        (str(_MODULE_PATH), str(wheel)),
        environment,
        _fake_load_native_module,
        run_command,
    )


def test_accepts_expected_release_wheel_tags(tmp_path: Path) -> None:
    wheel: Final = _write_wheel(tmp_path, filename_tag=_EXPECTED_TAG)

    assert _run_verifier(wheel) == 0


def test_rejects_cp312_version_specific_wheel(tmp_path: Path) -> None:
    tag: Final = "cp312-cp312-linux_x86_64"
    wheel: Final = _write_wheel(tmp_path, filename_tag=tag, metadata_tags=(tag,))

    assert _run_verifier(wheel) == 1


def test_rejects_non_linux_platform_tag(tmp_path: Path) -> None:
    tag: Final = "cp310-abi3-win_amd64"
    wheel: Final = _write_wheel(tmp_path, filename_tag=tag, metadata_tags=(tag,))

    assert _run_verifier(wheel) == 1


@pytest.mark.parametrize(
    "metadata_tags",
    (None, ("cp312-cp312-linux_x86_64",)),
    ids=("missing", "mismatched"),
)
def test_rejects_missing_or_mismatched_wheel_metadata_tag(
    tmp_path: Path,
    metadata_tags: tuple[str, ...] | None,
) -> None:
    wheel: Final = _write_wheel(tmp_path, filename_tag=_EXPECTED_TAG, metadata_tags=metadata_tags)

    assert _run_verifier(wheel) == 1


def test_rejects_wheel_metadata_from_wrong_dist_info_directory(
    tmp_path: Path,
) -> None:
    wheel: Final = _write_wheel(
        tmp_path,
        filename_tag=_EXPECTED_TAG,
        dist_info="decoy-1.0.0.dist-info",
    )

    assert _run_verifier(wheel) == 1


def test_rejects_duplicate_wheel_metadata_tags(tmp_path: Path) -> None:
    wheel: Final = _write_wheel(
        tmp_path,
        filename_tag=_EXPECTED_TAG,
        metadata_tags=(_EXPECTED_TAG, _EXPECTED_TAG),
    )

    assert _run_verifier(wheel) == 1


def test_rejects_duplicate_wheel_metadata_file(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        wheel: Final = _write_wheel(
            tmp_path,
            filename_tag=_EXPECTED_TAG,
            duplicate_wheel=True,
        )

    assert _run_verifier(wheel) == 1


def test_rejects_production_module_exposing_panic_hook(tmp_path: Path) -> None:
    wheel: Final = _write_wheel(tmp_path, filename_tag=_EXPECTED_TAG)

    assert _run_verifier(wheel, exposes_panic=True) == 1


@pytest.mark.parametrize("embedded", (False, True))
def test_vocabulary_is_packaged_once(tmp_path: Path, embedded: bool) -> None:
    ranks: Final = b"AA== 0\nAQ== 1\nAg== 2\n"
    wheel: Final = _write_wheel(
        tmp_path,
        filename_tag=_EXPECTED_TAG,
        native_bytes=b"native engine" + (ranks if embedded else b""),
    )
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("litellm/litellm_core_utils/tokenizers/" + "a" * 40, ranks)

    assert _run_verifier(wheel) == (1 if embedded else 0)


@pytest.mark.parametrize(
    ("platform", "glibc_versions"),
    (
        ("manylinux_2_39_x86_64", "GLIBC_2.39"),
        ("manylinux_2_39_x86_64", "GLIBC_2.9"),
        ("manylinux_2_17_x86_64", "GLIBC_2.2.5 GLIBC_2.17"),
        ("manylinux2014_x86_64", "GLIBC_2.17"),
        ("manylinux2010_x86_64", "GLIBC_2.12"),
        ("manylinux1_x86_64", "GLIBC_2.2.5"),
        ("manylinux_2_17_x86_64.manylinux2014_x86_64", "GLIBC_2.17"),
        ("linux_x86_64.manylinux_2_39_x86_64", "GLIBC_2.39"),
    ),
)
def test_accepts_linux_tags_that_cover_the_extensions_glibc_requirement(
    tmp_path: Path,
    platform: str,
    glibc_versions: str,
) -> None:
    tag: Final = "cp310-abi3-" + platform
    metadata: Final = tuple("cp310-abi3-" + item for item in platform.split("."))
    wheel: Final = _write_wheel(tmp_path, filename_tag=tag, metadata_tags=metadata)
    assert _run_verifier(wheel, glibc_versions=glibc_versions) == 0


@pytest.mark.parametrize(
    ("platform", "glibc_versions"),
    (
        ("manylinux_2_17_x86_64", "GLIBC_2.39"),
        ("manylinux2014_x86_64", "GLIBC_2.39"),
        ("manylinux_2_9_x86_64", "GLIBC_2.10"),
        ("manylinux_2_39_x86_64.manylinux2014_x86_64", "GLIBC_2.39"),
        ("manylinux_2_39_x86_64", ""),
        ("manylinux_2_39_aarch64", "GLIBC_2.39"),
        ("linux_x86_64.win_amd64", "GLIBC_2.39"),
        ("musllinux_1_2_x86_64", "GLIBC_2.39"),
        ("manylinux_2_unknown_x86_64", "GLIBC_2.39"),
    ),
)
def test_rejects_inaccurate_or_unverified_linux_compatibility_claims(
    tmp_path: Path,
    platform: str,
    glibc_versions: str,
) -> None:
    tag: Final = "cp310-abi3-" + platform
    metadata: Final = tuple("cp310-abi3-" + item for item in platform.split("."))
    wheel: Final = _write_wheel(tmp_path, filename_tag=tag, metadata_tags=metadata)
    assert _run_verifier(wheel, glibc_versions=glibc_versions) == 1
