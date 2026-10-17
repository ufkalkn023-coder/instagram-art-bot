import copy
import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

NOW = datetime(2026, 10, 19, 6, 23, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[1]


def publication(identifier, age=80, **changes):
    return {'id':identifier, 'media_id':'media-'+identifier, 'type':'single',
            'artwork_ids':['aic_1'], 'posted_at':(NOW-timedelta(hours=age)).isoformat(), **changes}


def build(history, snapshots=(), **kwargs):
    module = importlib.import_module('src.feed_weekly_report')
    return module.build_weekly_report({'status':'WAITING_COOLDOWN','next_format':'single'}, [],
                                    history, list(snapshots), now=kwargs.get('now',NOW))


def test_weekly_interval_is_half_open_and_offset_aware():
    history={'publications':[publication('start',168),publication('old',168.01),
                              publication('inside'),publication('end',0)]}
    before=copy.deepcopy(history)
    report=build(history)
    assert report['period']['start']=='2026-10-12T06:23:00Z'
    assert report['period']['end']=='2026-10-19T06:23:00Z'
    assert report['analytics']['summary']['feed_publications']==2
    assert {r['publication_id'] for r in report['analytics']['windows']}=={'start','inside'}
    assert history==before
    assert build(history,now=NOW.astimezone(timezone(timedelta(hours=3))))==report


def test_conflicting_identity_outside_period_cannot_make_inside_record_valid():
    history={'publications':[publication('same',80),publication('same',200),publication('valid')]}
    report=build(history)
    assert report['analytics']['summary']['feed_publications']==1
    assert report['source_diagnostics']['conflicting_publications']==2


def test_one_story_parent_counts_once_and_missing_rates_remain_unavailable():
    from tests.test_story_analytics import story_delivery
    story=publication('story',type='carousel',story_delivery=story_delivery(index=1,cover='detail_study'))
    report=build({'publications':[story]})
    assert report['analytics']['summary']['feed_publications']==1
    assert report['publication_counts']=={'single':0,'carousel':1}
    assert all(c['rates']['save_rate']['mean_rate'] is None for c in report['analytics']['cohorts'])
    text=importlib.import_module('src.feed_weekly_report').render_weekly_markdown(report)
    assert 'kullanılamıyor' in text
    assert 'minimum 5' in text
    assert 'kazanan' in text


def test_equal_target_rates_do_not_sum_or_merge_publication_ages():
    pub=publication('one')
    posted=NOW-timedelta(hours=80)
    snapshots=[{'publication_id':'one','media_id':'media-one','target_age_hours':age,
                'captured_at':(posted+timedelta(hours=age)).isoformat(),
                'metrics':{'reach':reach,'saved':saved}}
               for age,reach,saved in [(24,10,1),(72,20,4)]]
    report=build({'publications':[pub]},snapshots)
    cohorts={c['target_age_hours']:c for c in report['analytics']['cohorts'] if c['publication_format']=='single'}
    assert cohorts[24]['rates']['save_rate']['mean_rate']==0.1
    assert cohorts[72]['rates']['save_rate']['mean_rate']==0.2
    assert report['analytics']['summary']['feed_publications']==1


def test_naive_weekly_time_is_rejected():
    with pytest.raises(ValueError):
        build({'publications':[]},now=NOW.replace(tzinfo=None))


def input_files(tmp_path):
    paths={}
    for name,value in [('schedule',{'status':'READY'}),('history',{'publications':[publication('one')]}),('snapshots',[])]:
        path=tmp_path/(name+'.json')
        path.write_text(json.dumps(value))
        paths[name]=path
    return paths


def test_weekly_cli_writes_markdown_and_json_without_changing_inputs(tmp_path):
    command=importlib.import_module('scripts.report_feed_weekly')
    paths=input_files(tmp_path)
    before={key:p.read_bytes() for key,p in paths.items()}
    args=[part for key,p in paths.items() for part in ('--'+key,str(p))]
    output=tmp_path/'weekly.md'
    assert command.run(args+['--output',str(output),'--now',NOW.isoformat()])==0
    assert 'Haftalık Feed raporu' in output.read_text()
    assert json.loads(output.with_suffix('.json').read_text())['analytics']['summary']['feed_publications']==1
    assert all(p.read_bytes()==before[key] for key,p in paths.items())
    assert command.run(args+['--output',str(output),'--now',NOW.isoformat()])==1
    assert all(p.read_bytes()==before[key] for key,p in paths.items())


def test_shared_loader_can_skip_media_reads_for_reports(monkeypatch,tmp_path):
    import scripts.report_feed_dashboard as loader
    from tests.test_feed_queue import build as queue_fixture
    queue,_=queue_fixture(tmp_path/'queue')
    paths=input_files(tmp_path)
    args,now=loader.parse_dashboard_args([part for key,p in paths.items() for part in ('--'+key,str(p))]+[
        '--queue',str(queue.directory),'--output',str(tmp_path/'ignored.md'),'--now',NOW.isoformat()])
    def unexpected(*args,**kwargs):raise AssertionError('Report must not read thumbnail bytes')
    monkeypatch.setattr(loader,'_package_thumbnail',unexpected)
    result=loader.load_dashboard_inputs(args,now=now,include_thumbnails=False)
    assert len(result[1])==3
    assert all(row['thumbnail'] is None for row in result[1])


def test_weekly_workflow_is_main_read_only_and_artifact_only():
    workflow=yaml.safe_load((ROOT/'.github/workflows/feed_weekly_report.yml').read_text())
    assert workflow['on']['schedule']==[{'cron':'23 6 * * 1'}]
    assert workflow['permissions']=={'contents':'read'}
    job=workflow['jobs']['report']
    assert "github.ref == 'refs/heads/main'" in job['if']
    run=next(s for s in job['steps'] if 'report_feed_weekly.py' in s.get('run',''))
    assert '--r2' in run['run'] and '--expected-sha' in run['run']
    assert not any('INSTAGRAM' in key or 'GEMINI' in key for key in run['env'])
    assert any('upload-artifact@' in s.get('uses','') for s in job['steps'])
    assert any('GITHUB_STEP_SUMMARY' in s.get('run','') for s in job['steps'])


def test_existing_json_sidecar_blocks_all_output_without_overwriting(tmp_path):
    command=importlib.import_module('scripts.report_feed_weekly')
    paths=input_files(tmp_path)
    output=tmp_path/'weekly.md'
    output.with_suffix('.json').write_text('existing evidence')
    args=[part for key,p in paths.items() for part in ('--'+key,str(p))]
    assert command.run(args+['--output',str(output),'--now',NOW.isoformat()])==1
    assert not output.exists()
    assert output.with_suffix('.json').read_text()=='existing evidence'


def test_invalid_record_with_same_id_does_not_inflate_publication_totals():
    good=publication('same')
    invalid={**good,'media_id':None}
    report=build({'publications':[good,invalid]})
    assert report['analytics']['summary']['feed_publications']==1
    assert report['publication_counts']=={'single':1,'carousel':0}
