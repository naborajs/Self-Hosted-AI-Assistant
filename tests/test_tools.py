from tools.calculator import calculate_expression
from tools.file_manager import list_files
from tools.youtube_assistant import summarize_youtube_video


def test_calculator_basic():
    assert calculate_expression("2 + 2") == "4"


def test_file_manager_list():
    result = list_files(".")
    assert isinstance(result, str)


def test_youtube_assistant_parse():
    summary = summarize_youtube_video("https://youtu.be/dQw4w9WgXcQ")
    assert "YouTube Assistant" in summary
