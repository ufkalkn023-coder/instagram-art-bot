import copy
import importlib
from datetime import timedelta

from PIL import Image

from tests.test_feed_queue import NOW, content


def module():
    return importlib.import_module('src.feed_variety')


def test_repetition_counts_groups_not_individual_carousel_pages():
    candidate={'artist':'Claude Monet','museum':'Museum A','date':'1880','visual_color_family':'BLUE'}
    context=[{**candidate,'inventory_group':'one'} for _ in range(9)]
    before=copy.deepcopy(context)
    assert module().variety_adjustment(candidate,context)==-8
    assert module().variety_adjustment(candidate,context*3)==-8
    assert context==before


def test_unknowns_are_neutral_and_recent_group_pressure_is_bounded():
    assert module().variety_adjustment({'artist':'Unknown'},[{'artist':'Unknown'}])==0
    candidate={'artist':'Monet, Claude'}
    repeated=[{'artist':'Claude Monet','publication_id':str(i)} for i in range(30)]
    assert module().variety_adjustment(candidate,repeated)==-12
    recent=[{'artist':'Berthe Morisot','publication_id':'recent-'+str(i)} for i in range(12)]
    assert module().variety_adjustment(candidate,repeated+recent)==0


def test_inventory_context_excludes_used_expired_and_unverified_entries():
    packages=[{'id':str(i),'state':state,'expires_at':(NOW+delta).isoformat(),
               'content':{'artworks':[{'id':'aic_'+str(i),'artist':'Claude Monet'}]}}
              for i,(state,delta) in enumerate([('READY',timedelta(days=2)),('CONSUMED',timedelta(days=2)),
                                                ('CLAIMED',timedelta(days=2)),('READY',timedelta(days=-1))])]
    rows=module().context_from_inventory(packages,now=NOW)
    assert len(rows)==1 and rows[0]['id']=='aic_0'
    assert rows[0]['inventory_group']
    assert all('publication_id' not in row and 'posted_at' not in row for row in rows)


def test_planner_remembers_actual_prior_packages_without_changing_them(monkeypatch,tmp_path):
    from src.feed_editorial import FeedPairPlanner
    monkeypatch.setenv('ARTFOLIO_FEED_VARIETY_ENABLED','true')
    planner=FeedPairPlanner()
    contexts=[]
    def prepare(fmt,directory,excluded,**kwargs):
        contexts.append(kwargs.get('inventory_context',()))
        return content(directory,fmt,len(contexts))
    first=planner.prepare('carousel',tmp_path,set(),prepare=prepare)
    before=copy.deepcopy(first.artworks)
    planner.prepare('single',tmp_path,set(first.publication_ids),prepare=prepare)
    assert not contexts[0]
    assert {r['id'] for r in contexts[1]}==set(first.publication_ids)
    assert first.artworks==before


def test_planner_uses_seed_inventory_before_preparing_first_new_package(monkeypatch,tmp_path):
    from src.feed_editorial import FeedPairPlanner
    monkeypatch.setenv('ARTFOLIO_FEED_VARIETY_ENABLED','true')
    seed=({'id':'aic_existing','artist':'Claude Monet','inventory_group':'active'},)
    planner=FeedPairPlanner(inventory_context=seed)
    seen=[]
    def prepare(fmt,directory,excluded,**kwargs):
        seen.extend(kwargs['inventory_context'])
        return content(directory,fmt,3)
    planner.prepare('single',tmp_path,set(),prepare=prepare)
    assert seen==list(seed)


def test_single_ranking_avoids_pending_artist_without_extra_downloads(monkeypatch,tmp_path):
    from src import art_fetcher
    from src.quality_filter import ImageValidationResult
    from tests.test_single_post_diversity import _candidate
    candidates=[_candidate('a',artist='Claude Monet'),_candidate('z',artist='Berthe Morisot')]
    class Adapter:
        source_id='test'
        def fetch_candidates(self,**kwargs):return candidates
    calls=[]
    def download(url,output_path,**kwargs):
        calls.append(url)
        Image.new('RGB',(1600,1000),'navy').save(output_path)
        return ImageValidationResult(True,1600,1000,'JPEG','ok')
    monkeypatch.setenv('ARTFOLIO_FEED_VARIETY_ENABLED','true')
    monkeypatch.setattr(art_fetcher,'get_museum_adapters',lambda:[Adapter()])
    monkeypatch.setattr(art_fetcher.history_tracker,'get_recent_history',lambda:[])
    monkeypatch.setattr(art_fetcher.config,'OUTPUT_RAW_IMAGE_PATH',str(tmp_path/'raw.jpg'))
    monkeypatch.setattr(art_fetcher,'validate_and_download_image_with_metadata',download)
    monkeypatch.setattr(art_fetcher,'calculate_quality_score',lambda *args:90.0)
    monkeypatch.setattr(art_fetcher,'calculate_serendipity_bonus',lambda *args:0)
    pending=({'artist':'Claude Monet','inventory_group':'pending'},)
    rows=list(art_fetcher.iter_single_post_candidates(set(),max_candidates=2,inventory_context=pending))
    assert rows[0]['id']=='aic_z'
    assert len(calls)==2
    assert all(row['quality_score']==90 for row in rows)
    assert rows[1]['feed_variety_adjustment']==-4


def test_carousel_adjustment_preserves_quality_relevance_and_input():
    artwork={'id':'aic_1','artist':'Claude Monet','quality_score':90,'theme_relevance_score':80,'selection_score':85}
    before=copy.deepcopy(artwork)
    result=module().apply_variety(artwork,[{'artist':'Claude Monet','publication_id':'last'}])
    assert result['selection_score']==81
    assert result['quality_score']==90 and result['theme_relevance_score']==80
    assert artwork==before


def test_carousel_pipeline_applies_pending_context_after_existing_quality_gates(monkeypatch,tmp_path):
    from src import art_fetcher
    from src.carousel_themes import get_default_theme_registry
    from src.models import NormalizedArtwork
    from src.quality_filter import ImageValidationResult
    from src.theme_acquisition import ThemeAcquisitionPolicy
    theme=get_default_theme_registry().by_id('women_reading')
    candidates=[NormalizedArtwork(source='aic',source_id=str(i),title='Woman reading a book',
        artist_name=f'Artist {i}',creation_date='1880',medium='Oil on canvas',classification='Painting',
        museum_name=f'Museum {i%4}',image_url=f'https://images.example/{i}.jpg',
        image_width=1600,image_height=1200,is_public_domain=True,rights_status='CONFIRMED_PUBLIC_DOMAIN') for i in range(12)]
    class Adapter:
        source_id='test'
        def fetch_candidates(self,**kwargs):return candidates
    downloads=[]
    def download(url,output_path,**kwargs):
        downloads.append(url)
        Image.new('RGB',(1600,1200),'navy').save(output_path)
        return ImageValidationResult(True,1600,1200,'JPEG','ok')
    monkeypatch.setenv('ARTFOLIO_FEED_VARIETY_ENABLED','true')
    monkeypatch.setattr(art_fetcher.history_tracker,'get_recent_history',lambda:[])
    monkeypatch.setattr(art_fetcher.config,'DATA_DIR',str(tmp_path))
    monkeypatch.setattr(art_fetcher,'calculate_quality_score',lambda *args:90.0)
    monkeypatch.setattr('src.theme_acquisition.calculate_quality_score',lambda *args:90.0)
    monkeypatch.setattr(art_fetcher,'validate_and_download_image_with_metadata',download)
    pending=[{'museum':f'Museum {i}','inventory_group':'pending'} for i in range(4)]
    result=art_fetcher.fetch_themed_artworks(set(),'reading art',count=8,color_tone='warm',
        selection_run_seed=art_fetcher.SelectionRunSeed('variety','test'),theme_definition=theme,return_acquisition=True,
        acquisition_policy=ThemeAcquisitionPolicy(max_primary_queries=1,max_secondary_queries=0,minimum_safe_pool=9),
        adapters=(Adapter(),),inventory_context=pending)
    assert all(art['feed_variety_adjustment']==-2 for art in result.artworks)
    assert all(art['quality_score']==90 and art['theme_relevance_score']>=50 for art in result.artworks)
    assert len(downloads)<=12
