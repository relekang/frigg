# coding=utf-8
import os
import json
import logging
import traceback

from sys import platform as _platform

import yaml
import requests
from django.db import models
from django.conf import settings
from django.utils.functional import cached_property
from fabric.context_managers import lcd
from fabric.operations import local
from fabric.api import settings as fabric_settings

from frigg.utils import github_api_request
from .managers import ProjectManager


logger = logging.getLogger("frigg_build_logger")


class Project(models.Model):
    name = models.CharField(max_length=100, blank=True)
    owner = models.CharField(max_length=100, blank=True)
    git_repository = models.CharField(max_length=150, help_text="git@github.com:owner/repo.git")
    average_time = models.IntegerField(null=True)

    objects = ProjectManager()

    def __unicode__(self):
        return "%(owner)s / %(name)s " % self.__dict__

    @property
    def last_build_number(self):
        return self.builds.all().order_by('-build_number')[0]

    @property
    def working_directory(self):
        return os.path.join(settings.PROJECT_TMP_DIRECTORY, self.owner, self.name)


class BuildResult(models.Model):
    result_log = models.TextField()
    succeeded = models.BooleanField(default=False)
    return_code = models.CharField(max_length=100)

    def get_status(self):
        if self.succeeded:
            return "succeded"
        else:
            return "failed"

    def get_comment_message(self, url):
        if self.succeeded:
            return "All gooodie good\n\n%s" % url
        else:
            return "Be careful.. the tests failed\n\n%s" % url


class Build(models.Model):
    project = models.ForeignKey(Project, related_name='builds', null=True)
    build_number = models.IntegerField(db_index=True)
    pull_request_id = models.IntegerField(max_length=150, default=0)
    branch = models.CharField(max_length=100, default="master")
    sha = models.CharField(max_length=150)

    result = models.OneToOneField(BuildResult, null=True)

    class Meta:
        unique_together = ('project', 'build_number')

    def __unicode__(self):
        return "%(project)s / %(branch)s " % self.__dict__

    def get_absolute_url(self):
        return "https://%s/build/%s/" % (settings.SERVER_ADDRESS, self.id)

    def get_pull_request_url(self):
        if self.branch == "master":
            return "https://github.com/%s/%s/" % (self.get_owner(), self.get_name())

        return "https://github.com/%s/%s/pull/%s" % (self.get_owner(),
                                                     self.get_name(),
                                                     self.pull_request_id)

    def load_settings(self):
        path = os.path.join(self.working_directory, '.frigg.yml')
        # Default value for project .frigg.yml
        settings = {
            'webhooks': [],
            'comment': True
        }
        with open(path) as f:
            settings.update(yaml.load(f))
        return settings

    def run_tests(self):
        self._set_commit_status("pending")
        self._clone_repo()
        self.add_comment("Running tests.. be patient :)\n\n%s" %
                         self.get_absolute_url())
        build_result = BuildResult.objects.create()
        self.result = build_result
        self.save()
        try:

            for task in self.load_settings()['tasks']:
                self._run_task(task)
                if not self.result.succeeded:
                    # if one task fails, we do not care about the rest
                    break

        except AttributeError, e:
            self.result.succeeded = False
            self.result.result_log = str(e)
            self.result.save()
            self.add_comment("I was not able to perform the tests.. Sorry. \n "
                             "More information: \n\n %s" % str(e))

        self.add_comment(self.result.get_comment_message(self.get_absolute_url()))
        self._set_commit_status(self.result.get_status())

        for url in self.load_settings()['webhooks']:
            self.send_webhook(url)

    def deploy(self):
        with lcd(self.working_directory):
            local("./deploy.sh")

    def _clone_repo(self, depth=1):
        # Cleanup old if exists..
        self._delete_tmp_folder()
        local("mkdir -p %s" % settings.PROJECT_TMP_DIRECTORY)
        local("git clone --depth=%s --no-single-branch %s %s" % (
            depth,
            self.project.git_repository,
            self.working_directory
        ))

        with lcd(self.working_directory):
            local("git checkout %s" % self.branch)

    def _run_task(self, task_command):
        options = {
            'pwd': self.working_directory,
            'command': task_command
        }

        with fabric_settings(warn_only=True):
            with lcd(self.working_directory):
                if _platform == "darwin":
                    script_command = "script %(pwd)s/frigg_testlog %(command)s"
                else:
                    script_command = "script %(pwd)s/frigg_testlog -c \"%(command)s\" -q "

                run_result = local(script_command % options)
                run_result = local(task_command)

                self.result.succeeded = run_result.succeeded
                self.result.return_code += "%s," % run_result.return_code

                log = 'Task: %(command)s\n' % options
                log += '------------------------------------\n'

                with file("%(pwd)s/frigg_testlog" % options, "r") as f:
                    log += f.read() + "\n"

                log += '------------------------------------\n'
                log += 'Exited with exit code: %s\n\n' % run_result.return_code
                self.result.result_log += log
                self.result.save()

    def add_comment(self, message):
        if bool(self.load_settings().get('comment', True)):
            owner, repo = self.get_git_repo_owner_and_name()
            url = "%s/%s/commits/%s/comments" % (owner, repo, self.sha)
            github_api_request(url, {'body': message, 'sha': self.sha})

    def _set_commit_status(self, status, description="Done"):
        owner, repo = self.get_git_repo_owner_and_name()

        url = "%s/%s/statuses/%s" % (owner, repo, self.sha)

        data = {
            'state': status,
            'target_url': self.get_absolute_url(),
            'description': description,
            'context': 'build'
        }

        return github_api_request(url, data)

    def _delete_tmp_folder(self):
        if os.path.exists(self.working_directory):
            local("rm -rf %s" % self.working_directory)

    def testlog(self):
        try:
            with file("%s/frigg_testlog" % self.working_directory, "r") as f:
                return f.read()
        except IOError:
            logger.error(traceback.format_exc())
            return ""

    def send_webhook(self, url):
        return requests.post(url, data=json.dumps({
            'repository': self.project.git_repository,
            'sha': self.sha,
            'build_url': self.get_absolute_url(),
            'pull_request_id': self.pull_request_id,
            'state': self.result.succeeded,
            'return_code': self.result.return_code
        }))

    @cached_property
    def working_directory(self):
        return os.path.join(self.project.working_directory, str(self.id))
