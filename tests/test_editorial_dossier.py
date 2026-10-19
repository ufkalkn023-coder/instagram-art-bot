import pytest

from tests.test_editorial_workspace import projects


def dossier_data():
    return dict(title='Yakından uzağa', question='Resimlerde yakın yüzey ile uzak alan nasıl ilişki kuruyor?',
                contributions=[dict(project_id='p-01', contribution='İlk çalışma tekrar eden mavi izlerin yakın yüzeyini inceler.'),
                               dict(project_id='p-02', contribution='Detay sayfası küçük dikdörtgenleri tam resimle ilişkilendirir.'),
                               dict(project_id='p-03', contribution='Son çalışma büyük şekillerin çevresinde kalan boşluğa bakar.')])


def test_dossier_groups_three_to_five_explicit_projects_without_approval(tmp_path):
    from src.editorial_workspace import create_editorial_workspace
    result = create_editorial_workspace(tmp_path / 'workspace', projects(tmp_path), dossier=dossier_data())
    assert result['dossier']['question'] == dossier_data()['question']
    page = (tmp_path / 'workspace' / 'index.html').read_text()
    assert 'editorial-dossier' in page and 'Yakından uzağa' in page
    assert all(entry['contribution'] in page for entry in dossier_data()['contributions'])
    assert result['publication_ready'] is False


@pytest.mark.parametrize('kind', ['missing_project', 'duplicate_contribution', 'two_posts'])
def test_dossier_rejects_unknown_members_and_repeated_contributions(tmp_path, kind):
    from src.editorial_workspace import create_editorial_workspace
    data = dossier_data()
    if kind == 'missing_project':
        data['contributions'][0]['project_id'] = 'p-99'
    elif kind == 'duplicate_contribution':
        data['contributions'][1]['contribution'] = data['contributions'][0]['contribution']
    else:
        data['contributions'].pop()
    with pytest.raises(ValueError):
        create_editorial_workspace(tmp_path / 'bad', projects(tmp_path), dossier=data)
    assert not (tmp_path / 'bad').exists()
