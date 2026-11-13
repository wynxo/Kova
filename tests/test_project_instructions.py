"""Bounded project guidance: read KOVA.md / AGENTS.md without leaking binaries."""

from kova.project_instructions import project_instructions


def test_empty_when_no_guidance_files(tmp_path):
    assert project_instructions(tmp_path) == ""


def test_reads_kova_md_wrapped_with_header(tmp_path):
    (tmp_path / "KOVA.md").write_text("# Title\nbe nice\n", encoding="utf-8")
    out = project_instructions(tmp_path)
    assert out.startswith("Project instructions")
    assert "### KOVA.md" in out
    assert "be nice" in out


def test_reads_both_files_in_order(tmp_path):
    (tmp_path / "KOVA.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("beta", encoding="utf-8")
    out = project_instructions(tmp_path)
    assert out.index("### KOVA.md") < out.index("### AGENTS.md")
    assert "alpha" in out and "beta" in out


def test_skips_files_larger_than_the_read_cap(tmp_path):
    # 12001 bytes exceeds the 12000 read ceiling, so the file is skipped entirely.
    (tmp_path / "KOVA.md").write_bytes(b"a" * 12_001)
    assert project_instructions(tmp_path) == ""


def test_keeps_a_file_just_under_the_cap(tmp_path):
    (tmp_path / "KOVA.md").write_bytes(b"b" * 12_000)
    out = project_instructions(tmp_path)
    assert "### KOVA.md" in out


def test_skips_binary_files(tmp_path):
    (tmp_path / "KOVA.md").write_bytes(b"line1\n\x00line2")
    assert project_instructions(tmp_path) == ""


def test_output_is_capped_at_4096_chars(tmp_path):
    # ~126-char header + "### KOVA.md\n" + 4000 'x' = ~4138 > 4096, so it truncates.
    (tmp_path / "KOVA.md").write_text("x" * 4000, encoding="utf-8")
    out = project_instructions(tmp_path)
    assert len(out) == 4096
    assert out.startswith("Project instructions")


def test_block_content_is_stripped(tmp_path):
    (tmp_path / "KOVA.md").write_text("   spaced   \n\n", encoding="utf-8")
    out = project_instructions(tmp_path)
    assert "spaced" in out
    assert "   spaced" not in out
