from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('counters.urls')),
]

admin.site.site_header = 'counters'
admin.site.site_title = 'counters'
admin.site.index_title = 'Amministrazione'
