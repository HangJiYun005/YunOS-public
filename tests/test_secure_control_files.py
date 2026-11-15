from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from memory.secure_control_files import (
    SecureControlFileError,
    pinned_control_directory,
)


class SecureControlFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ayunos-secure-controls-",
            ignore_cleanup_errors=True,
        )
        self.addCleanup(self.temporary.cleanup)
        self.anchor = Path(self.temporary.name).resolve()

    def test_invalid_payload_and_partial_publication_leave_no_owned_root(self) -> None:
        invalid_root = self.anchor / "invalid"
        with self.assertRaisesRegex(
            SecureControlFileError, "secure_control_file_payload_invalid"
        ):
            with pinned_control_directory(
                anchor=self.anchor,
                path=invalid_root,
                create_if_missing=True,
            ) as directory:
                directory.create_file("control.json", "not-bytes")  # type: ignore[arg-type]
        self.assertFalse(invalid_root.exists())

        partial_root = self.anchor / "partial"
        with self.assertRaisesRegex(
            SecureControlFileError, "secure_control_file_exists"
        ):
            with pinned_control_directory(
                anchor=self.anchor,
                path=partial_root,
                create_if_missing=True,
            ) as directory:
                directory.create_file("control.json", b"first\n")
                directory.create_file("control.json", b"second\n")
        self.assertFalse(partial_root.exists())

    @unittest.skipUnless(os.name == "nt", "mandatory sharing is Windows")
    def test_windows_file_and_directory_pins_deny_mutation_until_close(self) -> None:
        root = self.anchor / "pinned"
        moved = self.anchor / "moved"
        with pinned_control_directory(
            anchor=self.anchor,
            path=root,
            create_if_missing=True,
        ) as directory:
            directory.create_file("control.json", b"sealed\n")
            with self.assertRaises(OSError):
                (root / "control.json").write_bytes(b"forged\n")
            with self.assertRaises(OSError):
                os.replace(root, moved)
            directory.verify()
        self.assertEqual(b"sealed\n", (root / "control.json").read_bytes())

    @unittest.skipIf(os.name == "nt", "FIFO probe is POSIX-only")
    def test_posix_read_pin_rejects_fifo_without_blocking(self) -> None:
        root = self.anchor / "fifo"
        root.mkdir()
        os.mkfifo(root / "control.json")
        with self.assertRaisesRegex(
            SecureControlFileError, "secure_control_file_identity_changed"
        ):
            with pinned_control_directory(
                anchor=self.anchor,
                path=root,
                create_if_missing=False,
            ) as directory:
                directory.open_file("control.json")


if __name__ == "__main__":
    unittest.main()
