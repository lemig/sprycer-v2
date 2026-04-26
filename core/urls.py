"""User-facing URLs.

Route paths preserved from legacy app (eng review Tension A): /imports,
/imports/new, /imports/<id>, /exports, /exports/new, /matchings,
/matchings/<id>/confirm, /matchings/<id>/reject. Schleiper's bookmarks
keep working after cutover.
"""
from django.urls import include, path

from . import views


imports_patterns = ([
    path('', views.imports_list, name='list'),
    path('new', views.imports_new, name='new'),
    path('<int:import_id>', views.imports_show, name='show'),
], 'imports')


exports_patterns = ([
    path('', views.exports_list, name='list'),
    path('new', views.exports_new, name='new'),
], 'exports')


matchings_patterns = ([
    path('', views.matchings_list, name='list'),
    path('<int:matching_id>/confirm', views.matchings_confirm, name='confirm'),
    path('<int:matching_id>/reject', views.matchings_reject, name='reject'),
], 'matchings')


urlpatterns = [
    path('', views.index, name='index'),
    path('imports/', include(imports_patterns)),
    path('exports/', include(exports_patterns)),
    path('matchings/', include(matchings_patterns)),
]
