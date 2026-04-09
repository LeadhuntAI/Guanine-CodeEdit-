"""Tests for FileScanner class."""

import os
import tempfile
import shutil
from pathlib import Path
import pytest

from file_merger import FileScanner


@pytest.fixture
def temp_source_dir():
    """Create a temporary directory with test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create some test files
        root = Path(tmpdir)
        (root / "file1.txt").write_text("Hello World\n", encoding="utf-8")
        (root / "file2.txt").write_text("Another file\nWith multiple lines\n", encoding="utf-8")
        (root / "subdir").mkdir()
        (root / "subdir" / "file3.txt").write_text("Nested file\n", encoding="utf-8")
        # Create a binary file (by writing bytes)
        (root / "binary.bin").write_bytes(b'\x00\x01\x02\x03')
        yield tmpdir


def test_file_scanner_initialization():
    """Test FileScanner can be initialized with default ignore patterns."""
    scanner = FileScanner()
    assert scanner.ignore_patterns is not None
    # Check some default patterns exist
    assert '.git' in scanner.ignore_patterns
    assert '__pycache__' in scanner.ignore_patterns


def test_should_ignore():
    """Test should_ignore method."""
    scanner = FileScanner(ignore_patterns=['.git', '*.pyc', '__pycache__'])
    assert scanner.should_ignore('.git') is True
    assert scanner.should_ignore('foo.pyc') is True
    assert scanner.should_ignore('__pycache__/foo.py') is True
    assert scanner.should_ignore('normal.txt') is False


def test_compute_hash():
    """Test hash computation."""
    with tempfile.NamedTemporaryFile(mode='wb', delete=False) as f:
        f.write(b'test content')
        fpath = f.name
    try:
        hash_val = FileScanner.compute_hash(fpath)
        # SHA-256 of 'test content' (without newline)
        expected = '6ae8a75555209fd6c44157c0aed8016e763ff435a19cf186f76863140143ff72'
        assert hash_val == expected
    finally:
        os.unlink(fpath)


def test_detect_binary():
    """Test binary detection."""
    with tempfile.NamedTemporaryFile(mode='wb', suffix='.txt', delete=False) as f:
        f.write(b'text file\n')
        fpath = f.name
    try:
        # Text file should not be binary
        assert FileScanner.detect_binary(fpath) is False
    finally:
        os.unlink(fpath)
    
    # Test with null byte
    with tempfile.NamedTemporaryFile(mode='wb', suffix='.txt', delete=False) as f:
        f.write(b'text\x00file\n')
        fpath = f.name
    try:
        # File with null byte should be detected as binary
        assert FileScanner.detect_binary(fpath) is True
    finally:
        os.unlink(fpath)


def test_scan_file(temp_source_dir):
    """Test scanning a single file."""
    scanner = FileScanner()
    root = Path(temp_source_dir)
    file_path = root / "file1.txt"
    
    version = scanner.scan_file(
        source_name="test_source",
        source_root=str(root),
        abs_path=str(file_path),
        rel_path="file1.txt"
    )
    
    assert version is not None
    assert version.source_name == "test_source"
    assert version.source_root == str(root)
    assert version.relative_path == "file1.txt"
    # File size depends on line endings (Windows adds \r\n)
    assert version.file_size >= 12  # At least 12 bytes
    assert version.line_count == 1  # One line
    assert version.is_binary is False
    assert version.sha256 == FileScanner.compute_hash(str(file_path))


def test_scan_source(temp_source_dir):
    """Test scanning an entire source directory."""
    scanner = FileScanner()
    # We need to import SourceConfig
    from file_merger import SourceConfig
    
    source = SourceConfig(name="test", path=temp_source_dir)
    result = scanner.scan_source(source)
    
    assert isinstance(result, dict)
    # Should have 4 files (file1.txt, file2.txt, subdir/file3.txt, binary.bin)
    assert len(result) == 4
    assert "file1.txt" in result
    assert "subdir/file3.txt" in result
    assert "binary.bin" in result
    
    # Verify binary file detection
    bin_version = result["binary.bin"]
    assert bin_version.is_binary is True
    assert bin_version.line_count is None  # Binary files shouldn't have line count