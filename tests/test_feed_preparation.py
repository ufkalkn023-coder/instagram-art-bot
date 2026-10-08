from types import SimpleNamespace

import main
from tests.test_single_feed_runtime import single_runtime


def test_single_preparation_returns_reusable_content_without_publication_state_mutation(monkeypatch, tmp_path):
    store, _, sent, artwork = single_runtime(monkeypatch, tmp_path)
    before = store.load_safety()[0].model_dump(mode="json")
    assert hasattr(main, "prepare_single_content"), "Reusable single preparation is missing"
    content = main.prepare_single_content(SimpleNamespace(dry_run=True))
    assert content.publication_format == "single"
    assert content.artworks[0]["id"] == artwork["id"]
    assert len(content.media_paths) == 1
    assert artwork["title"] in content.caption
    assert sent == []
    assert store.load_safety()[0].model_dump(mode="json") == before


def test_carousel_preparation_returns_cover_and_featured_assets_without_reservation(monkeypatch):
    from tests.test_carousel_orchestration import _install_reads_and_selection
    calls = []
    _install_reads_and_selection(monkeypatch, calls)
    monkeypatch.setattr(main, "create_carousel_editorial_cover", lambda **kwargs: kwargs["output_path"])
    monkeypatch.setattr(main, "render_carousel_featured_artwork", lambda *_, **kwargs: SimpleNamespace(output_path=kwargs["output_path"]))
    assert hasattr(main, "prepare_carousel_content"), "Reusable carousel preparation is missing"
    content = main.prepare_carousel_content(SimpleNamespace(dry_run=True, image_url=None, pinterest=False, prepare_only=True))
    assert content.publication_format == "carousel"
    assert 6 <= len(content.artworks) <= 9
    assert len(content.media_paths) == len(content.artworks)
    assert content.artworks[0]["id"] == "met_cover"
    assert content.publication_metadata["featured_count"] == len(content.artworks) - 1
    assert not any(item in calls for item in ("reserve", "upload", "publish"))


def test_controlled_caption_hook_reaches_rendered_caption_and_persistent_metadata(monkeypatch):
    from tests.test_carousel_orchestration import _install_reads_and_selection
    from src.models import CarouselExperimentMetadata
    _install_reads_and_selection(monkeypatch, [])
    monkeypatch.setenv('ARTFOLIO_CAPTION_EXPERIMENT_ENABLED', 'true')
    monkeypatch.setattr(main, 'create_carousel_editorial_cover', lambda **kwargs: kwargs['output_path'])
    monkeypatch.setattr(main, 'render_carousel_featured_artwork', lambda *_, **kwargs: SimpleNamespace(output_path=kwargs['output_path']))
    content = main.prepare_carousel_content(SimpleNamespace(dry_run=True, prepare_only=True, image_url=None, pinterest=False))
    metadata = CarouselExperimentMetadata.model_validate(content.publication_metadata)
    assignment = metadata.controlled_experiment
    assert assignment.variant == metadata.caption_hook_type
    expected = ('What connects these' if assignment.variant == 'question' else 'Look closely at these')
    assert expected in content.caption
