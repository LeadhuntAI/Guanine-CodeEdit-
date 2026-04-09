"""Basic test to verify pytest setup and core functionality."""

def test_pytest_works():
    """Test that pytest is correctly installed and running."""
    assert True

def test_import_file_merger():
    """Test that we can import the main module."""
    try:
        import file_merger
        assert True
    except ImportError as e:
        assert False, f"Failed to import file_merger: {e}"

def test_file_scanner_exists():
    """Test that FileScanner class exists."""
    import file_merger
    assert hasattr(file_merger, 'FileScanner'), "FileScanner class not found"