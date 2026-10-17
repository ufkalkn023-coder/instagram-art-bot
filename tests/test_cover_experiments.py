import copy
import importlib
import random
from types import SimpleNamespace

from PIL import Image, ImageChops
import pytest
from pydantic import ValidationError

from src.models import CarouselExperimentMetadata, PublicationRecord
from tests.test_carousel_history import _experiment_metadata


def assignment(variant='editorial',theme='winter_light'):
    return {'experiment_id':'cover_treatment_v1','variable':'cover_variant',
            'variant':variant,'theme_id':theme,'caption_hook_type':'visual_detail'}


def metadata(variant='editorial'):
    result=_experiment_metadata()
    result.update(cover_variant=variant,caption_hook_type='visual_detail',
                  controlled_cover_experiment=assignment(variant))
    result['engagement_features'].update(cover_variant=variant,caption_hook='visual_detail')
    return result


def test_cover_assignment_is_typed_frozen_and_roundtrips():
    from src import models
    model=models.ControlledCoverExperiment.model_validate(assignment())
    assert model.model_dump()==assignment()
    with pytest.raises(ValidationError):
        model.variant='editorial_panel'
    parsed=CarouselExperimentMetadata.model_validate(metadata('editorial_panel'))
    assert parsed.model_dump(exclude_none=True)['controlled_cover_experiment']==assignment('editorial_panel')


@pytest.mark.parametrize('changes',[{'cover_variant':'editorial_panel'},{'caption_hook_type':'question'},{'carousel_theme':'other'}])
def test_mismatched_cover_delivery_is_rejected(changes):
    value={**metadata(),**changes}
    with pytest.raises(ValidationError):
        CarouselExperimentMetadata.model_validate(value)


def test_both_assignments_and_story_treatments_are_rejected():
    value=metadata()
    value['controlled_experiment']={'experiment_id':'caption_hook_v1','variable':'caption_hook_type',
        'variant':'visual_detail','theme_id':'winter_light','cover_variant':'editorial'}
    with pytest.raises(ValidationError):
        CarouselExperimentMetadata.model_validate(value)
    from tests.test_story_analytics import publication,story_delivery
    story=publication(1,story_delivery(1))
    story.update(carousel_theme='winter_light',cover_variant='editorial',caption_hook_type='visual_detail',
                 controlled_cover_experiment=assignment())
    with pytest.raises(ValidationError):
        PublicationRecord.model_validate(story)


def test_family_assignment_is_deterministic_one_factor_and_rng_pure():
    from src.carousel_themes import get_default_theme_registry
    experiments=importlib.import_module('src.editorial_experiments')
    theme=get_default_theme_registry().themes[0]
    before=random.getstate()
    variants=set()
    for index in range(64):
        kwargs={'run_seed':str(index),'caption_enabled':True,'cover_enabled':True}
        first=experiments.assign_presentation_experiment(theme,**kwargs)
        assert first==experiments.assign_presentation_experiment(theme,**kwargs)
        variants.add((first.variable,first.variant))
    assert variants=={('caption_hook_type','question'),('caption_hook_type','visual_detail'),
                      ('cover_variant','editorial'),('cover_variant','editorial_panel')}
    assert random.getstate()==before
    assert experiments.assign_presentation_experiment(theme,run_seed='x',caption_enabled=False,cover_enabled=False) is None


def test_real_cover_arms_change_copy_panel_and_preserve_art_source(tmp_path):
    from src.carousel_cover import create_carousel_editorial_cover
    from src.carousel_plan import CoverMode
    from tests.test_carousel_cover import _cover_asset
    source=tmp_path/'source.jpg'
    Image.new('RGB',(1080,1350),(160,80,40)).save(source)
    original=source.read_bytes()
    outputs=[]
    for variant in ('editorial','editorial_panel'):
        path=tmp_path/(variant+'.jpg')
        create_carousel_editorial_cover(cover=_cover_asset(source,CoverMode.FULL_ARTWORK),
            editorial_title='LIGHT AND FORM',editorial_subtitle='Works across museum collections.',
            micro_facts=('6 works','1850–1900'),output_path=str(path),cover_variant=variant)
        with Image.open(path) as image:
            assert image.size==(1080,1350) and image.format=='JPEG'
            outputs.append(image.copy())
    assert ImageChops.difference(outputs[0],outputs[1]).getbbox() is not None
    assert ImageChops.difference(outputs[0].crop((0,200,1080,390)),outputs[1].crop((0,200,1080,390))).getbbox() is None
    assert source.read_bytes()==original
    bad=tmp_path/'bad'/'cover.jpg'
    with pytest.raises(ValueError):
        create_carousel_editorial_cover(cover=_cover_asset(source,CoverMode.FULL_ARTWORK),
            editorial_title='Title',editorial_subtitle='Subtitle',output_path=str(bad),cover_variant='unknown')
    assert not bad.exists()


def test_preparation_renders_assigned_cover_and_keeps_caption_identical(monkeypatch):
    import main
    from src import models
    from tests.test_carousel_orchestration import _install_reads_and_selection
    _install_reads_and_selection(monkeypatch,[])
    monkeypatch.setenv('ARTFOLIO_COVER_EXPERIMENT_ENABLED','true')
    monkeypatch.setattr(main.gemini_ai,'analyze_carousel',lambda *args,**kwargs:None)
    rendered=[]
    monkeypatch.setattr(main,'create_carousel_editorial_cover',lambda **kwargs:rendered.append(kwargs['cover_variant']) or kwargs['output_path'])
    monkeypatch.setattr(main,'render_carousel_featured_artwork',lambda *args,**kwargs:SimpleNamespace(output_path=kwargs['output_path']))
    captions=[]
    for variant in ('editorial','editorial_panel'):
        def choose(theme,**kwargs):
            return models.ControlledCoverExperiment.model_validate(assignment(variant,theme.id))
        monkeypatch.setattr(main,'assign_presentation_experiment',choose)
        result=main.prepare_carousel_content(SimpleNamespace(dry_run=True,prepare_only=True,image_url=None,pinterest=False))
        parsed=CarouselExperimentMetadata.model_validate(result.publication_metadata)
        assert parsed.cover_variant==variant and parsed.caption_hook_type=='visual_detail'
        assert parsed.controlled_cover_experiment.variant==variant
        assert parsed.controlled_experiment is None
        assert parsed.engagement_features.cover_variant==variant
        captions.append(result.caption)
    assert rendered==['editorial','editorial_panel']
    assert captions[0]==captions[1]


def test_cover_assignment_survives_reservation_and_confirmation(monkeypatch):
    from src import history_tracker
    from tests.test_carousel_history import _history_backend,_publication
    history,_=_history_backend(monkeypatch)
    cover,featured=_publication()
    value=metadata('editorial_panel')
    before=copy.deepcopy(value)
    identifier=history_tracker.reserve_carousel(cover,featured,theme_id='winter_light',
        theme_family='season',carousel_format='LIGHT_STUDY',publication_metadata=value)
    history_tracker.mark_artworks_publishing([cover['id'],*[art['id'] for art in featured]])
    history_tracker.confirm_carousel_publication(cover['id'],[art['id'] for art in featured],'media-cover')
    pub=history['publications'][0]
    assert pub['id']==identifier and pub['controlled_cover_experiment']==assignment('editorial_panel')
    assert PublicationRecord.model_validate(pub).controlled_cover_experiment.variant=='editorial_panel'
    assert value==before


def test_cover_cohorts_are_separate_from_caption_cohorts_and_never_promote():
    from src.feed_experiment_report import build_experiment_report
    from tests.test_feed_analytics import NOW,publication,snapshot
    pubs=[]
    for index,variant in enumerate(('editorial','editorial_panel')):
        pubs.append({**publication(index,'carousel'),'carousel_theme':'flowers','cover_variant':variant,
            'caption_hook_type':'visual_detail','controlled_cover_experiment':assignment(variant,'flowers')})
    pubs.append({**publication(2,'carousel'),'carousel_theme':'flowers','cover_variant':'editorial',
        'caption_hook_type':'question','controlled_experiment':{'experiment_id':'caption_hook_v1',
        'variable':'caption_hook_type','theme_id':'flowers','variant':'question','cover_variant':'editorial'}})
    report=build_experiment_report({'publications':pubs},[snapshot(p,metrics={'reach':100,'saved':0}) for p in pubs],now=NOW)
    cover=next(row for row in report['cover_comparisons'] if row['target_age_hours']==72)
    caption=next(row for row in report['comparisons'] if row['target_age_hours']==72)
    assert cover['caption_hook_type']=='visual_detail'
    assert cover['arms']['editorial']['save_rate']['observations']==1
    assert cover['arms']['editorial_panel']['like_rate']['observations']==0
    assert caption['arms']['question']['save_rate']['observations']==1
    assert caption['arms']['visual_detail']['save_rate']['observations']==0
    assert cover['winner'] is None and cover['status']=='insufficient_evidence'
    assert report['auto_promote'] is False


def test_weekly_report_exposes_separate_experiment_evidence():
    from src.feed_weekly_report import build_weekly_report,render_weekly_markdown
    from tests.test_feed_analytics import NOW,publication,snapshot
    pub={**publication(0,'carousel'),'carousel_theme':'flowers','cover_variant':'editorial_panel',
         'caption_hook_type':'visual_detail','controlled_cover_experiment':assignment('editorial_panel','flowers')}
    report=build_weekly_report({'status':'WAITING_COOLDOWN'},[],{'publications':[pub]},[snapshot(pub)],now=NOW)
    assert report['experiments']['cover_comparisons']
    assert not report['experiments']['comparisons']
    assert 'Deney gözlemleri' in render_weekly_markdown(report)
