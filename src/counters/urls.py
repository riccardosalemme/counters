from django.contrib.auth import views as auth_views
from django.urls import path

from . import views_api, views_display

# Routes carry no trailing slash: /add/caffe/12 has to work as typed, without a
# redirect. The value is matched as <str> so that -10, 1.5 and 1,5 all pass.
urlpatterns = [
    path('get', views_api.get_batch, name='get-batch'),
    path('get/<slug:slug>', views_api.get_counter, name='get-counter'),
    path('add/<slug:slug>/<str:value>', views_api.add_counter, name='add-counter'),
    path('subtract/<slug:slug>/<str:value>', views_api.subtract_counter, name='subtract-counter'),
    path('set/<slug:slug>/<str:value>', views_api.set_counter, name='set-counter'),
    path('set', views_api.set_batch, name='set-batch'),
    path('batch', views_api.set_batch, name='batch'),
    path('display/<slug:slug>', views_display.display, name='display'),

    # Its own login, because the admin one turns away anyone without is_staff.
    path('login', auth_views.LoginView.as_view(template_name='counters/login.html'), name='login'),
    path('logout', auth_views.LogoutView.as_view(), name='logout'),
    path('', views_display.index, name='index'),
]
