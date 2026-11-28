"""Scheduled Feed must create the new presentation without pre-approved stock."""
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.feed_content import PreparedFeedContent
from tests.test_story_suggestions import make_sources


@pytest.fixture(autouse=True)
def editorial_model_response(monkeypatch):
    """Replace only paid inference; source validation, copy checks and rendering stay real."""
    from src import gemini_ai
    class Models:
        def generate_content(self, **kwargs):
            metadata = json.loads(kwargs['contents'][1])
            return SimpleNamespace(text=json.dumps({
                'title': 'Blue shapes on an ivory ground',
                'angle': 'Overlapping rectangular edges organize the visible composition.',
                'lead': ('Blue rectangles overlap across an ivory ground. Their repeated edges '
                         'form a diagonal rhythm, while the pale spaces keep each shift in position visible.'),
                'notes': [{'artwork_id': art['artwork_id'], 'heading': 'Overlapping edges',
                           'text': 'Each blue rectangle partly covers the next, creating a stepped pattern against the pale background.'}
                          for art in metadata['artworks']],
            }))
    client = SimpleNamespace(models=Models(), close=lambda: None)
    monkeypatch.setenv('GOOGLE_GEMINI_API_KEY', 'test-key')
    monkeypatch.setattr(gemini_ai, '_create_client', lambda _key: client)
    return client.models


def test_generated_copy_replaces_raw_catalogue_title_and_materials(tmp_path):
    from dataclasses import replace
    from src.automatic_editorial import prepare_automatic_editorial
    acquired = acquired_content(tmp_path, 'single')
    art = dict(acquired.artworks[0], title='Fragment van een muurschildering', medium='loam; dye; strohalm')
    content = prepare_automatic_editorial(replace(acquired, artworks=(art,)), tmp_path / 'rendered')
    assert content.story_delivery.public_title == 'Blue shapes on an ivory ground'
    assert 'Blue rectangles overlap across an ivory ground.' in content.caption
    assert 'loam; dye; strohalm' not in content.caption
    assert 'Fragment van een muurschildering — Known Artist' in content.caption
    assert content.story_delivery.headline_kind == 'ai_proposal'


@pytest.mark.parametrize('failure', ['missing_key', 'disabled', 'unavailable', 'malformed', 'wrong_source', 'raw_catalogue'])
def test_unusable_editorial_ai_never_publishes_raw_metadata(monkeypatch, tmp_path, editorial_model_response, failure):
    from src import gemini_ai
    from src.automatic_editorial import prepare_automatic_editorial
    acquired = acquired_content(tmp_path, 'single')
    if failure == 'missing_key':
        monkeypatch.delenv('GOOGLE_GEMINI_API_KEY')
    elif failure == 'disabled':
        monkeypatch.setattr(gemini_ai.config, 'GEMINI_ENABLED', False)
    elif failure == 'unavailable':
        def unavailable(**_kwargs):
            raise OSError('provider unavailable')
        monkeypatch.setattr(editorial_model_response, 'generate_content', unavailable)
    else:
        response = {'title': 'Blue shapes on an ivory ground', 'angle': 'Repeated edges create a diagonal rhythm.',
                    'lead': 'Blue rectangles overlap across an ivory ground, creating repeated diagonal steps across the image.',
                    'notes': [{'artwork_id': 'cleveland_0', 'heading': 'Repeated edges',
                               'text': 'The rectangles overlap in a diagonal sequence against an ivory background.'}]}
        if failure == 'wrong_source':
            response['notes'][0]['artwork_id'] = 'cleveland_other'
        elif failure == 'raw_catalogue':
            response.update(title='Painting 0', lead='Painting 0')
        else:
            response = {}
        monkeypatch.setattr(editorial_model_response, 'generate_content',
                            lambda **_kwargs: SimpleNamespace(text=json.dumps(response)))
    with pytest.raises((ValueError, RuntimeError)):
        prepare_automatic_editorial(acquired, tmp_path / 'rendered')
    assert not list((tmp_path / 'rendered').glob('editorial-*.jpg'))


def acquired_content(tmp_path, format_name, *, offset=0):
    sources = make_sources(tmp_path, 1 if format_name == 'single' else 6)
    artworks = tuple(dict(source.artwork.model_dump(mode='json'), id=f'cleveland_{index + offset}',
                          source_id=str(index + offset),
                          artist=source.artwork.artist_name, date=source.artwork.creation_date,
                          museum=source.artwork.museum_name, local_image_path=source.image_path)
                     for index, source in enumerate(sources))
    return PreparedFeedContent(format_name, artworks, tuple(s.image_path for s in sources),
                               'Recorded museum credits', theme_id='flowers_in_painting')


@pytest.mark.parametrize('format_name,count', [('single', 1), ('carousel', 5)])
def test_automatic_render_uses_new_editorial_pages_and_truthful_provenance(tmp_path, format_name, count):
    from src.automatic_editorial import prepare_automatic_editorial
    from src.story_feed import validate_story_media
    prepared = acquired_content(tmp_path, format_name)
    content = prepare_automatic_editorial(prepared, tmp_path / 'rendered')
    assert content.publication_format == format_name
    assert len(content.media_paths) == count
    assert content.story_delivery.schema_version == 'artfolio-story-delivery-v3'
    assert content.story_delivery.headline_kind in {'ai_proposal'}
    assert content.story_delivery.cover_variant == 'museum_journal'
    assert all(page.role == 'artwork' for page in content.story_delivery.pages)
    expected_ids = prepared.publication_ids if format_name == 'single' else prepared.featured_ids
    assert content.publication_ids == expected_ids
    assert 'Known Artist' in content.caption and 'Cleveland' in content.caption
    assert not list(tmp_path.rglob('.story-review.json'))
    for path in content.media_paths:
        with Image.open(path) as image:
            assert image.size == (1080, 1350)
            assert image.getpixel((0, 0)) != (0, 0, 128)
    validate_story_media(content)


@pytest.mark.parametrize('failure', ['rights', 'duplicate', 'identity', 'image'])
def test_automatic_generation_rejects_invalid_source_before_publication(tmp_path, failure):
    from dataclasses import replace
    from src.automatic_editorial import prepare_automatic_editorial
    content = acquired_content(tmp_path, 'single')
    art = dict(content.artworks[0])
    if failure == 'rights':
        art.update(is_public_domain=False, rights_status='UNKNOWN', license=None)
    elif failure == 'duplicate':
        content = acquired_content(tmp_path, 'carousel')
        rows = list(content.artworks)
        rows[-1] = dict(rows[1])
        content = replace(content, artworks=tuple(rows))
    elif failure == 'identity':
        art['source'] = 'aic'
    else:
        Path(art['local_image_path']).write_bytes(b'invalid image')
    if failure != 'duplicate':
        content = replace(content, artworks=(art,))
    with pytest.raises((ValueError, OSError)):
        prepare_automatic_editorial(content, tmp_path / 'rendered')


def test_source_provenance_uses_the_same_private_bytes_as_rendering(monkeypatch, tmp_path):
    import hashlib
    from src import automatic_editorial
    acquired = acquired_content(tmp_path, 'single')
    raw = Path(acquired.artworks[0]['local_image_path'])
    original_digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    render = automatic_editorial.render_editorial_page
    def replace_original_after_render(*args, **kwargs):
        result = render(*args, **kwargs)
        Image.new('RGB', (1600, 1800), 'red').save(raw)
        return result
    monkeypatch.setattr(automatic_editorial, 'render_editorial_page', replace_original_after_render)
    content = automatic_editorial.prepare_automatic_editorial(acquired, tmp_path / 'rendered')
    assert content.story_delivery.source_sha256['cleveland_0'] == original_digest


@pytest.mark.parametrize('phase,status,publish_calls', [('success', 'PUBLISHED', 1),
                                                     ('ambiguous', 'AMBIGUOUS', 1)])
@pytest.mark.parametrize('backend', ['none', 'local', 'r2'])
def test_empty_stock_generates_and_uses_real_guarded_publication(monkeypatch, tmp_path, phase, status, publish_calls, backend):
    import main
    from tests.test_feed_schedule import NOW
    from tests.test_single_feed_runtime import single_runtime
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path, phase=phase)
    prepared = acquired_content(tmp_path, 'single')
    # Only acquisition is substituted; the renderer, admission, CAS and receipt remain real.
    monkeypatch.setattr(main, 'prepare_single_content', lambda *_args, **_kwargs: prepared)
    monkeypatch.setenv('ARTFOLIO_FEED_CONTENT_MODE', 'automatic')
    monkeypatch.delenv('ARTFOLIO_FEED_QUEUE_ENABLED', raising=False)
    options = []
    if backend == 'local':
        options = ['--prepared-queue', str(tmp_path / 'empty-queue')]
    elif backend == 'r2':
        from src import r2_feed_queue
        from tests.test_r2_feed_queue import remote
        queue = remote(tmp_path)
        monkeypatch.setattr(r2_feed_queue, 'R2PreparedFeedQueue', lambda *_: queue)
        options = ['--prepared-queue-r2']
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(main, 'datetime', Clock)
    assert main.main(['--mode', 'single', *options]) == (0 if phase == 'success' else 1)
    assert sum(url.endswith('/media_publish') for url, _ in sent) == publish_calls
    state = store.load_safety()[0]
    assert state.active_publication_state.posted_artworks[0]['status'] == status
    if phase == 'success':
        delivery = store.load_receipts()[0].records[-1].story_delivery
        assert delivery.schema_version == 'artfolio-story-delivery-v3'
        assert delivery.cover_variant == 'museum_journal'
    else:
        assert state.feed_schedule_control.paused


def test_automatic_mode_never_acquires_without_a_schedule_permit(monkeypatch, tmp_path):
    import main
    from src.feed_schedule import FeedScheduleManager
    from tests.test_feed_schedule import NOW
    from tests.test_single_feed_runtime import single_runtime
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv('ARTFOLIO_FEED_CONTENT_MODE', 'automatic')
    FeedScheduleManager(store, owned_media=lambda **_: set()).pause(
        expected_generation=3, reason='stopped', now=NOW)
    monkeypatch.setattr(main, 'prepare_single_content', lambda *_args, **_kwargs: pytest.fail('unpermitted acquisition'))
    assert main.main(['--mode', 'single']) == 1
    assert sent == []


def test_bad_queue_is_not_hidden_by_fresh_generation(monkeypatch, tmp_path):
    import main
    from tests.test_feed_schedule import auth
    directory = tmp_path / 'broken'
    directory.mkdir()
    (directory / 'queue.json').write_text('{broken')
    monkeypatch.setenv('ARTFOLIO_FEED_CONTENT_MODE', 'automatic')
    monkeypatch.setattr(main.history_tracker, 'get_posted_ids', lambda: set())
    monkeypatch.setattr(main, 'prepare_single_content', lambda *_args, **_kwargs: pytest.fail('unsafe fallback'))
    with pytest.raises((ValueError, RuntimeError)):
        main.run_feed_with_queue(SimpleNamespace(dry_run=False, prepared_queue=directory),
                                 main.ProductionMode.SINGLE, auth())


def test_automatic_carousel_publishes_new_pages_with_one_boundary_and_exact_receipt(monkeypatch, tmp_path):
    import main
    from src import instagram_poster
    from tests.test_single_feed_runtime import single_runtime
    from tests.test_instagram_poster import FakeResponse
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv('ARTFOLIO_FEED_CONTENT_MODE', 'automatic')
    monkeypatch.delenv('ARTFOLIO_FEED_QUEUE_ENABLED', raising=False)
    acquired = acquired_content(tmp_path, 'carousel')
    monkeypatch.setattr(main, 'prepare_carousel_content', lambda *_args, **_kwargs: acquired)
    def post(url, **kwargs):
        sent.append((url, kwargs['data']))
        return FakeResponse(200, {'id': 'published-carousel' if url.endswith('/media_publish') else f'container-{len(sent)}'})
    monkeypatch.setattr(instagram_poster.requests, 'post', post)
    assert main.main(['--mode', 'carousel']) == 0
    assert sum(url.endswith('/media_publish') for url, _ in sent) == 1
    assert len([data for _, data in sent if data.get('is_carousel_item') == 'true']) == 5
    delivery = store.load_receipts()[0].records[-1].story_delivery
    assert delivery.source_ids == ('cleveland_1', 'cleveland_2', 'cleveland_3', 'cleveland_4', 'cleveland_5')
    assert delivery.schema_version == 'artfolio-story-delivery-v3'
    assert len(store.load_safety()[0].active_publication_state.posted_artworks) == 5


def test_automatic_mode_prefers_existing_reviewed_editorial_without_generation(monkeypatch, tmp_path):
    import main
    from src.feed_queue import PreparedFeedQueue
    from src.story_feed import prepare_story_content
    from tests.test_editorial_production import editorial_projects
    from tests.test_feed_queue import NOW
    from tests.test_single_feed_runtime import single_runtime
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    contents = iter(prepare_story_content(path) for path in editorial_projects(inputs))
    queue = PreparedFeedQueue(tmp_path / 'queue')
    queue.build(target=3, first_format='single', prepare=lambda *_: next(contents), now=NOW)
    monkeypatch.setenv('ARTFOLIO_FEED_CONTENT_MODE', 'automatic')
    monkeypatch.setattr(main, 'prepare_single_content', lambda *_args, **_kwargs: pytest.fail('unneeded generation'))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(main, 'datetime', Clock)
    assert main.main(['--mode', 'single', '--prepared-queue', str(queue.directory)]) == 0
    assert queue.status()[0]['state'] == 'CONSUMED'
    assert store.load_receipts()[0].records[-1].story_delivery.schema_version == 'artfolio-story-delivery-v2'
    assert sum(url.endswith('/media_publish') for url, _ in sent) == 1


@pytest.mark.parametrize('backend', ['local', 'r2'])
def test_automatic_mode_skips_legacy_stock_without_changing_it(monkeypatch, tmp_path, backend):
    import main
    from tests.test_feed_queue import NOW, build
    from tests.test_single_feed_runtime import single_runtime
    single_runtime(monkeypatch, tmp_path)
    queue, _ = build(tmp_path)
    options = ['--prepared-queue', str(queue.directory)]
    if backend == 'r2':
        from src import r2_feed_queue
        from tests.test_r2_feed_queue import remote
        remote_queue = remote(tmp_path)
        remote_queue.install(queue, now=NOW)
        queue = remote_queue
        monkeypatch.setattr(r2_feed_queue, 'R2PreparedFeedQueue', lambda *_: queue)
        options = ['--prepared-queue-r2']
    before = queue.status()
    acquired = acquired_content(tmp_path, 'single')
    monkeypatch.setattr(main, 'prepare_single_content', lambda *_args, **_kwargs: acquired)
    monkeypatch.setenv('ARTFOLIO_FEED_CONTENT_MODE', 'automatic')
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(main, 'datetime', Clock)
    assert main.main(['--mode', 'single', *options]) == 0
    assert queue.status() == before


def test_stock_check_reports_on_demand_generation_instead_of_requiring_review(monkeypatch, tmp_path, capsys):
    import json
    from scripts import prepare_feed_queue as command
    from tests.test_feed_queue_refill import cli_args, wire_command
    from tests.test_r2_feed_queue import remote
    queue = remote(tmp_path)
    _, client = wire_command(monkeypatch, queue)
    monkeypatch.setenv('ARTFOLIO_FEED_CONTENT_MODE', 'automatic')
    before = client.puts
    assert command.run(cli_args(tmp_path, '--check-refill')) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['reason'] == 'ON_DEMAND_GENERATION_AVAILABLE'
    assert result['requires_editorial_review'] is False
    assert result['on_demand_generation'] is True
    assert client.puts == before and not queue.client.writes


def test_generated_single_is_included_in_learning_and_analytics(tmp_path):
    from src.automatic_editorial import prepare_automatic_editorial
    from src.engagement_features import EngagementFeatureVector
    from src.feed_analytics import build_feed_analytics_report
    from tests.test_story_analytics import NOW, publication, snapshot
    content = prepare_automatic_editorial(acquired_content(tmp_path, 'single'), tmp_path / 'rendered')
    delivery = content.story_delivery.model_dump(mode='json')
    features = EngagementFeatureVector.from_context({'story_delivery': delivery, 'publication_format': 'single'})
    assert features.narrative == 'single_study'
    assert features.page_count == 1
    assert features.headline_kind == 'ai_proposal'
    pub = dict(publication(delivery=delivery), type='single')
    report = build_feed_analytics_report({'publications': [pub]}, [snapshot(pub)], now=NOW)
    row = next(row for row in report['windows'] if row['target_age_hours'] == 72)
    assert row['story_delivery']['page_count'] == 1


@pytest.mark.parametrize('backend', ['local', 'r2'])
def test_generated_packages_survive_queue_storage_and_claim(tmp_path, backend):
    from src.automatic_editorial import prepare_automatic_editorial
    from src.feed_queue import PreparedFeedQueue
    from src.story_feed import validate_story_media
    from tests.test_feed_queue import NOW
    queue = PreparedFeedQueue(tmp_path / 'queue')
    sequence = 0
    def prepare(format_name, directory, excluded):
        from dataclasses import replace
        nonlocal sequence
        sequence += 10
        inputs = directory / 'inputs'
        inputs.mkdir()
        acquired = acquired_content(inputs, format_name, offset=sequence)
        acquired = replace(acquired, artworks=tuple(dict(art, rights_text='CC0 public domain',
                            copyright_notice='No copyright restrictions', image_width=1600, image_height=1800)
                            for art in acquired.artworks))
        return prepare_automatic_editorial(acquired, directory / 'editorial')
    queue.build(target=3, first_format='single', prepare=prepare, now=NOW)
    if backend == 'r2':
        from tests.test_r2_feed_queue import remote
        remote_queue = remote(tmp_path)
        remote_queue.install(queue, now=NOW)
        queue = remote_queue
    claim = queue.claim('single', protected_ids=set(), owner='run:automatic', now=NOW)
    assert claim.content.publication_ids == ('cleveland_10',)
    assert claim.content.story_delivery.schema_version == 'artfolio-story-delivery-v3'
    assert all('local_image_path' not in artwork for artwork in claim.content.artworks)
    assert claim.content.artworks[0]['rights_text'] == 'CC0 public domain'
    assert claim.content.artworks[0]['copyright_notice'] == 'No copyright restrictions'
    assert claim.content.artworks[0]['image_width'] == 1600
    assert claim.content.artworks[0]['image_height'] == 1800
    validate_story_media(claim.content)


def test_gemini_request_uses_default_model_images_and_one_bounded_call(tmp_path, monkeypatch, editorial_model_response):
    from src.automatic_editorial import prepare_automatic_editorial
    original = editorial_model_response.generate_content
    requests = []
    def capture(**kwargs):
        requests.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(editorial_model_response, 'generate_content', capture)
    prepare_automatic_editorial(acquired_content(tmp_path, 'carousel'), tmp_path / 'rendered')
    assert len(requests) == 1
    request = requests[0]
    assert request['model'] == 'gemini-3.8-flash'
    assert len([part for part in request['contents'] if getattr(part, 'inline_data', None)]) == 5
    assert request['config'].max_output_tokens == 4096
    assert request['config'].thinking_config.thinking_level == 'MEDIUM'


def test_ai_failure_stops_before_any_instagram_request(monkeypatch, tmp_path):
    import main
    from tests.test_single_feed_runtime import single_runtime
    store, _, sent, _ = single_runtime(monkeypatch, tmp_path)
    prepared = acquired_content(tmp_path, 'single')
    monkeypatch.setattr(main, 'prepare_single_content', lambda *_args, **_kwargs: prepared)
    monkeypatch.setenv('ARTFOLIO_FEED_CONTENT_MODE', 'automatic')
    monkeypatch.delenv('ARTFOLIO_FEED_QUEUE_ENABLED', raising=False)
    monkeypatch.delenv('GOOGLE_GEMINI_API_KEY')
    assert main.main(['--mode', 'single']) == 1
    assert sent == []
    assert store.load_safety()[0].active_publication_state.posted_artworks == []


def test_gemini_verification_renders_both_formats_without_publication(tmp_path, monkeypatch, capsys):
    from scripts import verify_gemini
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    sources = make_sources(inputs, 6)
    monkeypatch.setattr(verify_gemini, 'acquire_sources', lambda *_args, **_kwargs: sources)
    output = tmp_path / 'probe'
    assert verify_gemini.run(['--output', str(output), '--format', 'both']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['model'] == 'gemini-3.8-flash'
    assert report['inference_calls'] == 2 and report['publications'] == 0
    assert [row['pages'] for row in report['results']] == [1, 5]
    assert (output / 'single' / 'caption.txt').is_file()
    assert (output / 'carousel' / 'editorial-05.jpg').is_file()


def test_automatic_feed_does_not_reuse_catalogue_only_v3_stock(tmp_path):
    from dataclasses import replace
    from src.automatic_editorial import prepare_automatic_editorial
    from src.feed_content_policy import is_automatic_editorial_content
    content = prepare_automatic_editorial(acquired_content(tmp_path, 'single'), tmp_path / 'rendered')
    assert is_automatic_editorial_content(content)
    delivery = content.story_delivery.model_copy(update={'headline_kind': 'source_title'})
    stale = replace(content, story_delivery=delivery, publication_metadata={'story_delivery': delivery.model_dump(mode='json')})
    assert not is_automatic_editorial_content(stale)


def test_provider_rejection_reports_reason_without_exposing_api_key(monkeypatch, tmp_path, editorial_model_response, caplog):
    from google.genai import errors
    from src.automatic_editorial import prepare_automatic_editorial
    def rejected(**_kwargs):
        raise errors.ClientError(400, {'error': {'message': 'Invalid JSON schema; key=test-key',
                                                'status': 'INVALID_ARGUMENT'}})
    monkeypatch.setattr(editorial_model_response, 'generate_content', rejected)
    with pytest.raises(RuntimeError):
        prepare_automatic_editorial(acquired_content(tmp_path, 'single'), tmp_path / 'rendered')
    assert '400' in caplog.text and 'Invalid JSON schema' in caplog.text
    assert 'test-key' not in caplog.text


def test_strict_copy_schema_is_sent_as_json_schema_through_real_sdk(monkeypatch, tmp_path):
    import httpx
    from google import genai
    from google.genai import types
    from src import gemini_ai
    from src.automatic_editorial import prepare_automatic_editorial
    requests = []
    response = {'title': 'Blue rectangles in a diagonal rhythm', 'angle': 'Overlapping edges build a stepped composition.',
                'lead': 'Blue rectangles overlap across the pale ground. Their repeated edges form a diagonal sequence, with narrow ivory gaps separating the shifting shapes.',
                'notes': [{'artwork_id': 'cleveland_0', 'heading': 'Stepped edges',
                           'text': 'Overlapping blue shapes create a stepped pattern of straight edges against the ivory ground.'}]}
    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        generation = payload['generationConfig']
        if 'responseSchema' in generation:
            # The OpenAPI Schema endpoint has no additionalProperties field.
            return httpx.Response(400, json={'error': {'message': 'Unsupported field additionalProperties in responseSchema.',
                                                       'status': 'INVALID_ARGUMENT'}})
        return httpx.Response(200, json={'candidates': [{'content': {'role': 'model', 'parts': [{'text': json.dumps(response)}]},
                                                       'finishReason': 'STOP'}]})
    def client(key):
        return genai.Client(api_key=key, http_options=types.HttpOptions(
            client_args={'transport': httpx.MockTransport(handle)},
            retry_options=types.HttpRetryOptions(attempts=1)))
    monkeypatch.setattr(gemini_ai, '_create_client', client)
    content = prepare_automatic_editorial(acquired_content(tmp_path, 'single'), tmp_path / 'rendered')
    assert content.story_delivery.public_title == 'Blue rectangles in a diagonal rhythm'
    assert len(requests) == 1
    schema = requests[0]['generationConfig']['responseJsonSchema']
    assert schema['additionalProperties'] is False


def _fixed_carousel_selection(tmp_path):
    import main
    from src.artwork_visual_features import extract_visual_features
    from src.carousel_plan import CoverAsset, CoverMode, CoverScoreBreakdown
    from src.carousel_themes import get_default_theme_registry
    from src.editorial_experiments import CaptionHookType

    sources = make_sources(tmp_path, 6)
    artworks = [
        dict(
            source.artwork.model_dump(mode='json'),
            id=f'cleveland_{index}',
            source_id=str(index),
            artist=source.artwork.artist_name,
            date=source.artwork.creation_date,
            museum=source.artwork.museum_name,
            local_image_path=source.image_path,
        )
        for index, source in enumerate(sources)
    ]
    cover_art = artworks[0]
    featured = artworks[1:]
    cover = CoverAsset(
        artwork=cover_art,
        local_image_path=cover_art['local_image_path'],
        mode=CoverMode.FULL_ARTWORK,
        cover_score=90.0,
        score_breakdown=CoverScoreBreakdown(
            theme_relevance=20.0,
            technical_quality=20.0,
            resolution=15.0,
            composition_suitability=10.0,
            crop_flexibility=10.0,
            visual_readability=10.0,
            aspect_ratio=5.0,
        ),
        visual_features=extract_visual_features(cover_art['local_image_path']),
    )
    theme = get_default_theme_registry().enabled_themes[0]
    return main._SelectedCarousel(
        featured, cover, theme, CaptionHookType.CURIOSITY, None,
    )


def test_automatic_carousel_skips_redundant_legacy_render_with_equivalent_delivery(
    monkeypatch, tmp_path
):
    import main
    from src.automatic_editorial import prepare_automatic_editorial
    from src.instagram_image import inspect_instagram_image_publishability
    from src.story_feed import validate_story_media

    sources_dir = tmp_path / 'sources'
    sources_dir.mkdir()
    selected = _fixed_carousel_selection(sources_dir)

    monkeypatch.setattr(main.history_tracker, 'get_posted_ids', lambda: set())
    monkeypatch.setattr(main.history_tracker, 'get_recent_carousel_theme_history', lambda: [])
    monkeypatch.setattr(main, '_get_grid_color_tone_for_run', lambda _dry_run: 'warm')
    monkeypatch.setattr(main, '_select_carousel_artworks', lambda *_args, **_kwargs: selected)
    monkeypatch.setattr(main.gemini_ai, 'analyze_carousel', lambda *_args, **_kwargs: None)
    monkeypatch.setenv('ARTFOLIO_SELECTION_SEED', 'tour3-equivalence-seed')

    # Baseline: legacy render enabled (writes 1 cover + 5 featured intermediate JPGs).
    legacy_prep_dir = tmp_path / 'legacy_prep'
    legacy_prep_dir.mkdir()
    legacy_args = SimpleNamespace(
        dry_run=True, prepare_only=True, automatic_editorial=False,
        preparation_directory=legacy_prep_dir,
    )
    baseline_acquired = main.prepare_carousel_content(legacy_args)
    assert len(list(legacy_prep_dir.glob('carousel_*.jpg'))) == 6
    baseline_delivery = prepare_automatic_editorial(baseline_acquired, tmp_path / 'baseline_editorial')

    # Optimized automatic editorial path: skips all 6 intermediate legacy JPG renders.
    auto_prep_dir = tmp_path / 'auto_prep'
    auto_prep_dir.mkdir()
    render_calls = []
    monkeypatch.setattr(
        main, '_render_carousel_content',
        lambda *args, **kwargs: render_calls.append((args, kwargs)),
    )
    auto_args = SimpleNamespace(
        dry_run=True, prepare_only=True, automatic_editorial=True,
        preparation_directory=auto_prep_dir,
    )
    auto_acquired = main.prepare_carousel_content(auto_args)
    assert render_calls == []
    assert list(auto_prep_dir.glob('carousel_*.jpg')) == []
    assert auto_acquired.publication_ids == baseline_acquired.publication_ids
    assert auto_acquired.featured_ids == baseline_acquired.featured_ids
    assert auto_acquired.theme_id == baseline_acquired.theme_id
    assert auto_acquired.theme_family == baseline_acquired.theme_family
    assert auto_acquired.carousel_format == baseline_acquired.carousel_format

    auto_delivery = prepare_automatic_editorial(auto_acquired, tmp_path / 'auto_editorial')
    validate_story_media(auto_delivery)
    assert len(auto_delivery.media_paths) == 5
    for path in auto_delivery.media_paths:
        assert inspect_instagram_image_publishability(path).publishable

    assert auto_delivery.publication_ids == baseline_delivery.publication_ids
    assert auto_delivery.caption == baseline_delivery.caption
    assert auto_delivery.theme_id == baseline_delivery.theme_id
    assert auto_delivery.theme_family == baseline_delivery.theme_family
    assert auto_delivery.carousel_format == baseline_delivery.carousel_format
    assert auto_delivery.story_delivery == baseline_delivery.story_delivery
    assert [Path(p).read_bytes() for p in auto_delivery.media_paths] == [
        Path(p).read_bytes() for p in baseline_delivery.media_paths
    ]


def test_automatic_queue_preparation_skips_legacy_carousel_render(monkeypatch, tmp_path):
    import main
    from scripts import prepare_feed_queue as command
    from src.automatic_editorial import prepare_automatic_editorial

    sources_dir = tmp_path / 'sources'
    sources_dir.mkdir()
    selected = _fixed_carousel_selection(sources_dir)

    monkeypatch.setattr(main.history_tracker, 'get_posted_ids', lambda: set())
    monkeypatch.setattr(main.history_tracker, 'get_recent_carousel_theme_history', lambda: [])
    monkeypatch.setattr(main, '_get_grid_color_tone_for_run', lambda _dry_run: 'warm')
    monkeypatch.setattr(main, '_select_carousel_artworks', lambda *_args, **_kwargs: selected)
    monkeypatch.setattr(
        main, '_render_carousel_content',
        lambda *_args, **_kwargs: pytest.fail('legacy carousel render should be skipped'),
    )

    prep_dir = tmp_path / 'queue_slot'
    prep_dir.mkdir()
    acquired = command._prepare('carousel', prep_dir, set(), automatic_editorial=True)
    assert list(prep_dir.glob('carousel_*.jpg')) == []
    content = prepare_automatic_editorial(acquired, prep_dir / 'editorial')
    assert content.story_delivery.schema_version == 'artfolio-story-delivery-v3'
    assert len(content.media_paths) == 5


def test_automatic_single_rejects_unpublishable_first_candidate_and_fails_when_exhausted(
    monkeypatch, tmp_path
):
    import main
    from src.automatic_editorial import prepare_automatic_editorial
    from src.instagram_image import inspect_instagram_image_publishability

    sources_dir = tmp_path / 'single_sources'
    sources_dir.mkdir()
    template_source = make_sources(sources_dir, 1)[0]
    bad_path = tmp_path / 'bad_aspect.jpg'
    Image.new('RGB', (2000, 200), (120, 40, 40)).save(bad_path, format='JPEG', quality=90)
    good_path = tmp_path / 'good_portrait.jpg'
    Image.new('RGB', (1200, 1500), (40, 80, 140)).save(good_path, format='JPEG', quality=90)

    def _candidate(cid: str, path: Path) -> dict:
        return dict(
            template_source.artwork.model_dump(mode='json'),
            id=cid,
            source='cleveland',
            source_id=cid.split('_', 1)[1],
            title=f'Work {cid}',
            artist=template_source.artwork.artist_name,
            date=template_source.artwork.creation_date,
            museum=template_source.artwork.museum_name,
            local_image_path=str(path),
        )

    bad = _candidate('cleveland_101', bad_path)
    good = _candidate('cleveland_102', good_path)

    monkeypatch.setattr(main.history_tracker, 'get_posted_ids', lambda: set())
    monkeypatch.setattr(
        main.art_fetcher,
        'iter_single_post_candidates',
        lambda *_args, **_kwargs: (item for item in [bad, good]),
    )
    monkeypatch.setattr(
        main.gemini_ai, 'analyze_artwork',
        lambda *_args, **_kwargs: pytest.fail('legacy single gemini should be skipped'),
    )

    prep_dir = tmp_path / 'single_prep'
    prep_dir.mkdir()
    args = SimpleNamespace(
        dry_run=True, prepare_only=True, automatic_editorial=True,
        preparation_directory=prep_dir,
    )
    acquired = main.prepare_single_content(args)
    assert acquired.publication_ids == ('cleveland_102',)
    assert list(prep_dir.glob('single_*.jpg')) == []

    content = prepare_automatic_editorial(acquired, tmp_path / 'single_editorial')
    assert content.publication_ids == ('cleveland_102',)
    assert len(content.media_paths) == 1
    assert inspect_instagram_image_publishability(content.media_paths[0]).publishable

    # Exhausted pool (no publishable candidate) must fail closed.
    monkeypatch.setattr(
        main.art_fetcher,
        'iter_single_post_candidates',
        lambda *_args, **_kwargs: (item for item in [bad]),
    )
    with pytest.raises(RuntimeError, match='Instagram image validation'):
        main.prepare_single_content(args)


def test_run_fresh_feed_carousel_hides_raw_paths_and_fails_on_unreadable_cover(
    monkeypatch, tmp_path
):
    import main
    from src.feed_content import PreparedStoryContent
    from src.story_feed import validate_story_media

    sources_dir = tmp_path / 'sources'
    sources_dir.mkdir()
    selected = _fixed_carousel_selection(sources_dir)

    monkeypatch.setenv('ARTFOLIO_FEED_CONTENT_MODE', 'automatic')
    monkeypatch.setattr(main.history_tracker, 'get_posted_ids', lambda: set())
    monkeypatch.setattr(main.history_tracker, 'get_recent_carousel_theme_history', lambda: [])
    monkeypatch.setattr(main, '_get_grid_color_tone_for_run', lambda _dry_run: 'warm')
    monkeypatch.setattr(main, '_select_carousel_artworks', lambda *_args, **_kwargs: selected)
    monkeypatch.setattr(
        main, '_render_carousel_content',
        lambda *_args, **_kwargs: pytest.fail('legacy carousel render must not run'),
    )

    captured = {}

    def fake_run_carousel_post(args, authorization=None, *, prepared_content=None):
        captured['content'] = prepared_content
        captured['media_names'] = [Path(p).name for p in prepared_content.media_paths]
        captured['media_bytes'] = [Path(p).read_bytes() for p in prepared_content.media_paths]
        validate_story_media(prepared_content)
        return 'ok'

    monkeypatch.setattr(main, 'run_carousel_post', fake_run_carousel_post)
    args = SimpleNamespace(dry_run=True, content_mode='automatic', mode='carousel')
    assert main._run_fresh_feed(args, main.ProductionMode.CAROUSEL, None) == 'ok'

    content = captured['content']
    assert isinstance(content, PreparedStoryContent)
    assert content.story_delivery.schema_version == 'artfolio-story-delivery-v3'
    assert captured['media_names'] == [f'editorial-0{i}.jpg' for i in range(1, 6)]
    raw_paths = {selected.cover.local_image_path, *(a['local_image_path'] for a in selected.artworks)}
    assert not raw_paths.intersection(content.media_paths)
    assert all('local_image_path' not in art for art in content.artworks)

    # Error parity: unreadable cover slot must stop before publication.
    Path(selected.cover.local_image_path).write_bytes(b'not-a-valid-image')
    captured.clear()
    with pytest.raises(OSError):
        main._run_fresh_feed(args, main.ProductionMode.CAROUSEL, None)
    assert captured == {}


def test_prepare_feed_queue_cli_automatic_carousel_hides_raw_paths_and_fails_on_bad_cover(
    monkeypatch, tmp_path
):
    import main
    from scripts import prepare_feed_queue as command
    from src.feed_queue import PreparedFeedQueue
    from src.story_feed import validate_story_media

    sources_dir = tmp_path / 'sources'
    sources_dir.mkdir()
    selected = _fixed_carousel_selection(sources_dir)

    single_dir = tmp_path / 'single_src'
    single_dir.mkdir()
    single_source = make_sources(single_dir, 1)[0]
    single_candidate = dict(
        single_source.artwork.model_dump(mode='json'),
        id='cleveland_900',
        source='cleveland',
        source_id='900',
        artist=single_source.artwork.artist_name,
        date=single_source.artwork.creation_date,
        museum=single_source.artwork.museum_name,
        local_image_path=single_source.image_path,
    )

    monkeypatch.setattr(main.history_tracker, 'get_posted_ids', lambda: set())
    monkeypatch.setattr(main.history_tracker, 'get_recent_carousel_theme_history', lambda: [])
    monkeypatch.setattr(main, '_get_grid_color_tone_for_run', lambda _dry_run: 'warm')
    monkeypatch.setattr(main, '_select_carousel_artworks', lambda *_args, **_kwargs: selected)
    monkeypatch.setattr(
        main, '_render_carousel_content',
        lambda *_args, **_kwargs: pytest.fail('legacy carousel render must not run'),
    )
    monkeypatch.setattr(
        main.art_fetcher,
        'iter_single_post_candidates',
        lambda *_args, **_kwargs: (item for item in [single_candidate]),
    )

    # Build a 3-package queue starting with carousel: carousel, single, carousel.
    # Provide a second distinct carousel set for slot 3 so artworks don't collide.
    sources_dir_2 = tmp_path / 'sources_2'
    sources_dir_2.mkdir()
    selected_2 = _fixed_carousel_selection(sources_dir_2)
    shifted_featured = [
        dict(art, id=f'cleveland_{idx + 20}', source_id=str(idx + 20))
        for idx, art in enumerate(selected_2.artworks, start=1)
    ]
    shifted_cover_art = dict(selected_2.cover.artwork, id='cleveland_20', source_id='20')
    from dataclasses import replace as dc_replace
    selected_2 = main._SelectedCarousel(
        shifted_featured,
        dc_replace(selected_2.cover, artwork=shifted_cover_art),
        selected_2.theme,
        selected_2.caption_hook_type,
        None,
    )
    carousel_iter = iter([selected, selected_2])
    monkeypatch.setattr(main, '_select_carousel_artworks', lambda *_args, **_kwargs: next(carousel_iter))

    queue_dir = tmp_path / 'auto_queue'
    rc = command.run([
        '--directory', str(queue_dir),
        '--target', '3',
        '--first-format', 'carousel',
        '--content-mode', 'automatic',
        '--skip-keychain',
    ])
    assert rc == 0
    assert list(queue_dir.rglob('carousel_*.jpg')) == []

    manifest_text = (queue_dir / 'queue.json').read_text(encoding='utf-8')
    assert str(sources_dir) not in manifest_text
    assert 'local_image_path' not in manifest_text

    queue = PreparedFeedQueue(queue_dir)
    claim = queue.claim('carousel', protected_ids=set(), owner='run:test')
    assert claim is not None
    assert claim.content.story_delivery.schema_version == 'artfolio-story-delivery-v3'
    assert len(claim.content.media_paths) == 5
    assert all('local_image_path' not in art for art in claim.content.artworks)
    validate_story_media(claim.content)

    # Error parity: unreadable cover fails closed and leaves no queue manifest.
    Path(selected.cover.local_image_path).write_bytes(b'corrupted-cover')
    monkeypatch.setattr(main, '_select_carousel_artworks', lambda *_args, **_kwargs: selected)
    bad_queue_dir = tmp_path / 'bad_auto_queue'
    with pytest.raises(SystemExit) as exc:
        command.run([
            '--directory', str(bad_queue_dir),
            '--target', '3',
            '--first-format', 'carousel',
            '--content-mode', 'automatic',
            '--skip-keychain',
        ])
    assert exc.value.code == 1
    assert not (bad_queue_dir / 'queue.json').exists()
