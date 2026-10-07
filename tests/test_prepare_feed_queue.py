import importlib
import json

from tests.test_feed_queue import content


def test_preparation_command_builds_alternating_local_packages_and_restores_configuration(monkeypatch, tmp_path, capsys):
    assert importlib.util.find_spec("scripts.prepare_feed_queue") is not None, "Prepared Feed queue CLI is missing"
    command = importlib.import_module("scripts.prepare_feed_queue")
    original_data = command.config.DATA_DIR
    original_raw = command.config.OUTPUT_RAW_IMAGE_PATH
    seen = []
    def prepare(args, *, excluded_ids):
        assert args.dry_run and args.prepare_only
        assert str(args.preparation_directory) == command.config.DATA_DIR
        format_name = "single" if len(seen) == 1 else "carousel"
        seen.append(format_name)
        return content(args.preparation_directory, format_name, len(seen))
    monkeypatch.setattr(command.main, "prepare_single_content", prepare)
    monkeypatch.setattr(command.main, "prepare_carousel_content", prepare)
    directory = tmp_path / "queue"
    assert command.run(["--directory", str(directory), "--target", "3", "--first-format", "carousel", "--skip-keychain"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [item["publication_format"] for item in result] == ["carousel", "single", "carousel"]
    assert command.config.DATA_DIR == original_data and command.config.OUTPUT_RAW_IMAGE_PATH == original_raw
