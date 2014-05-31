from django.conf.urls import patterns, include, url

from django.contrib import admin
admin.autodiscover()

urlpatterns = patterns('',
    # Examples:
    # url(r'^$', 'frigg.views.home', name='home'),
    # url(r'^blog/', include('blog.urls')),

    url(r'^$', 'frigg.builds.views.overview'),

    url(r'^build/(?P<build_id>\d+)/$', 'frigg.builds.views.build', name="view_build"),
    url(r'^deploy/(?P<build_id>\d+)/$', 'frigg.builds.views.deploy_master_branch', name="deploy_master_branch"),

    url(r'^github-webhook/', 'frigg.builds.views.github_webhook'),
    url(r'^admin/', include(admin.site.urls)),
)
