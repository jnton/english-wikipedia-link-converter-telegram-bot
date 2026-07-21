import argparse
import struct
from pathlib import Path

ELF_MACHINE_NAMES = {
    62: "x86_64",
    183: "arm64",
}
EXPECTED_MACHINE = {
    "x86_64": 62,
    "arm64": 183,
}


def elf_machine(path: Path) -> int | None:
    data = path.read_bytes()[:20]
    if len(data) < 20 or data[:4] != b"\x7fELF":
        return None
    byte_order = data[5]
    if byte_order == 1:
        return struct.unpack("<H", data[18:20])[0]
    if byte_order == 2:
        return struct.unpack(">H", data[18:20])[0]
    raise ValueError(f"Unknown ELF byte order in {path}")


def verify_package(root: Path, architecture: str) -> list[Path]:
    expected = EXPECTED_MACHINE[architecture]
    native_files: list[Path] = []
    mismatches: list[str] = []

    for path in sorted(root.rglob("*.so")):
        machine = elf_machine(path)
        if machine is None:
            continue
        native_files.append(path)
        if machine != expected:
            actual = ELF_MACHINE_NAMES.get(machine, f"ELF machine {machine}")
            mismatches.append(f"{path}: expected {architecture}, found {actual}")

    if mismatches:
        raise RuntimeError("Native Lambda package architecture mismatch:\n" + "\n".join(mismatches))
    if not native_files:
        raise RuntimeError(
            "No native extensions were found; architecture verification did not exercise the compiled dependencies."
        )
    return native_files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("architecture", choices=sorted(EXPECTED_MACHINE))
    args = parser.parse_args()

    files = verify_package(args.package, args.architecture)
    print(f"Verified {len(files)} native extensions for {args.architecture}.")


if __name__ == "__main__":
    main()
