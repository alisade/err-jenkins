# coding: utf-8
from __future__ import (absolute_import, division,
                        print_function, unicode_literals)
from xml.etree import ElementTree as et
import validators
import io
import re
import os
from itertools import chain
import requests

from jinja2 import Template
from jenkins import Jenkins, JenkinsException, LAUNCHER_JNLP
from errbot import BotPlugin, botcmd, webhook
from errbot import ValidationException
import time

API_TIMEOUT = 5  # Timeout to connect to the AWS metadata service

try:
    from config import JENKINS_URL, JENKINS_USERNAME, JENKINS_PASSWORD
except ImportError:
    # Default mandatory configuration
    JENKINS_URL = ''
    JENKINS_USERNAME = None
    JENKINS_PASSWORD = None

try:
    from config import (JENKINS_RECEIVE_NOTIFICATION,
                        JENKINS_CHATROOMS_NOTIFICATION)
except ImportError:
    # Default optional configuration
    JENKINS_RECEIVE_NOTIFICATION = True
    JENKINS_CHATROOMS_NOTIFICATION = ()


CONFIG_TEMPLATE = {
    'URL': JENKINS_URL,
    'USERNAME': JENKINS_USERNAME,
    'PASSWORD': JENKINS_PASSWORD,
    'RECEIVE_NOTIFICATION': JENKINS_RECEIVE_NOTIFICATION,
    'CHATROOMS_NOTIFICATION': JENKINS_CHATROOMS_NOTIFICATION}

JENKINS_JOB_TEMPLATE_PIPELINE = """<?xml version='1.0' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <actions/>
  <description></description>
  <keepDependencies>false</keepDependencies>
  <properties/>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition" plugin="workflow-cps">
    <scm class="hudson.plugins.git.GitSCM" plugin="git">
      <configVersion>2</configVersion>
      <userRemoteConfigs>
        <hudson.plugins.git.UserRemoteConfig>
          <url>{repository}</url>
        </hudson.plugins.git.UserRemoteConfig>
      </userRemoteConfigs>
      <branches>
        <hudson.plugins.git.BranchSpec>
          <name>*/master</name>
        </hudson.plugins.git.BranchSpec>
      </branches>
      <doGenerateSubmoduleConfigurations>false</doGenerateSubmoduleConfigurations>
      <submoduleCfg class="list"/>
      <extensions/>
    </scm>
    <scriptPath>Jenkinsfile</scriptPath>
  </definition>
  <triggers/>
</flow-definition>
"""

JENKINS_JOB_TEMPLATE_MULTIBRANCH = """<?xml version='1.0' encoding='UTF-8'?>
<org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject plugin="workflow-multibranch">
  <actions/>
  <description></description>
  <properties>
    <com.cloudbees.hudson.plugins.folder.properties.FolderCredentialsProvider_-FolderCredentialsProperty plugin="cloudbees-folder">
      <domainCredentialsMap class="hudson.util.CopyOnWriteMap$Hash">
        <entry>
          <com.cloudbees.plugins.credentials.domains.Domain plugin="credentials">
            <specifications/>
          </com.cloudbees.plugins.credentials.domains.Domain>
          <java.util.concurrent.CopyOnWriteArrayList/>
        </entry>
      </domainCredentialsMap>
    </com.cloudbees.hudson.plugins.folder.properties.FolderCredentialsProvider_-FolderCredentialsProperty>
  </properties>
  <views>
    <hudson.model.AllView>
      <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../../.."/>
      <name>All</name>
      <filterExecutors>false</filterExecutors>
      <filterQueue>false</filterQueue>
      <properties class="hudson.model.View$PropertyList"/>
    </hudson.model.AllView>
  </views>
  <viewsTabBar class="hudson.views.DefaultViewsTabBar"/>
  <primaryView>All</primaryView>
  <healthMetrics>
    <com.cloudbees.hudson.plugins.folder.health.WorstChildHealthMetric plugin="cloudbees-folder"/>
  </healthMetrics>
  <icon class="com.cloudbees.hudson.plugins.folder.icons.StockFolderIcon" plugin="cloudbees-folder"/>
  <orphanedItemStrategy class="com.cloudbees.hudson.plugins.folder.computed.DefaultOrphanedItemStrategy" plugin="cloudbees-folder">
    <pruneDeadBranches>true</pruneDeadBranches>
    <daysToKeep>0</daysToKeep>
    <numToKeep>5</numToKeep>
  </orphanedItemStrategy>
  <triggers>
    <com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger plugin="cloudbees-folder">
      <spec>*/12 * * * *</spec>
      <interval>300000</interval>
    </com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger>
  </triggers>
  <sources class="jenkins.branch.MultiBranchProject$BranchSourceList" plugin="branch-api">
    <data>
      <jenkins.branch.BranchSource>
        <source class="org.jenkinsci.plugins.github_branch_source.GitHubSCMSource" plugin="github-branch-source">
          <repoOwner>{repo_owner}</repoOwner>
          <repository>{repo_name}</repository>
          <includes>*</includes>
          <excludes></excludes>
        </source>
        <strategy class="jenkins.branch.DefaultBranchPropertyStrategy">
          <properties class="empty-list"/>
        </strategy>
      </jenkins.branch.BranchSource>
    </data>
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
  </sources>
  <factory class="org.jenkinsci.plugins.workflow.multibranch.WorkflowBranchProjectFactory">
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
  </factory>
</org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>"""


class JenkinsBot(BotPlugin):
    """Basic Err integration with Jenkins CI"""

    min_err_version = '1.2.1'
    # max_err_version = '4.0.3'
    def get_configuration_template(self):
        return CONFIG_TEMPLATE

    def configure(self, configuration):
        if configuration is not None and configuration != {}:
            config = dict(chain(CONFIG_TEMPLATE.items(),
                                configuration.items()))
        else:
            config = CONFIG_TEMPLATE
        super(JenkinsBot, self).configure(config)
        return

    def check_configuration(self, configuration):
        self.log.debug(configuration)
        for c, v in configuration.items():
            if c == 'URL':
                if not validators.url(v):
                    raise ValidationException('JENKINS_URL is not a well formed URL')
            elif c in ['RECEIVE_NOTIFICATION']:
                if len(v) == 0 or not isinstance(v, str):
                    raise ValidationException("{} is a required string config setting".format(c))
            elif c in ['CHATROOMS_NOTIFICATION']:
                if not isinstance(v, tuple):
                    raise ValidationException("{} should be of type tuple".format(c))
        return

    def connect_to_jenkins(self, grid):
        self.set_jenkins_url(grid)
        """Connect to a Jenkins instance using configuration."""
        self.log.debug('Connecting to Jenkins ({0})'.format(
            self.config['URL']))
        self.jenkins = Jenkins(url=self.config['URL'],
                               username=self.config['USERNAME'],
                               password=self.config['PASSWORD'])
        return

    def broadcast(self, mess, use_card):
        """Shortcut to broadcast a message to all elligible chatrooms."""
        chatrooms = (self.config['CHATROOMS_NOTIFICATION']
                     or self.config['GRID_NOTIFICATION']
                     or self.bot_config.CHATROOM_PRESENCE)

        for room in chatrooms:
            if use_card:
                mess['to'] = self.build_identifier(room)
                self.send_card(**mess)
            else:
                self.send(self.build_identifier(room), mess)
        return

    def set_jenkins_url(self, grid):
        """deploy to grid with the same name as the slack channel"""
        domain = os.environ['DOMAIN']
        url = 'http://slave-' + grid + '.' + domain + ':3000/scripts/jenkins_url'
        for i in range(0,4):
            try:
                resp = requests.get(url, timeout=API_TIMEOUT).json()
            #except requests.exceptions.ConnectTimeout:
            except Exception as e:
                self.log.warning('Connection timeout to Instance API endpoint: ' + str(e))
                self.config['URL'] = None
                time.sleep(5)
                continue
            else:
                self.config['URL'] = 'http://' + resp['stdout'][0]
                break

    @webhook(r'/jenkins/notification')
    def handle_notification(self, incoming_request):
        if not self.config['RECEIVE_NOTIFICATION']:
            return 'Notification handling is disabled.'

        self.log.debug(repr(incoming_request))
        # parse incoming request url to find
        # the grid/channelname to post
        url = incoming_request['build']['full_url']
        m = re.match(r'https://master-(.*).' + os.environ['DOMAIN'], url)
        grid  = m.group(1)
        grid_channel = '#' + grid or '#deploy'
        self.config['GRID_NOTIFICATION'] = ( grid_channel,)
        job_name = incoming_request['name']
        build_number = incoming_request['build']['number']
        self.connect_to_jenkins(grid)

        build_info = self.jenkins.get_build_info(job_name, build_number)
        bi = build_info['actions']
        for i in range(0, len(bi)):
            if bi[i].get('lastBuiltRevision') != None:
                git_commit_id = bi[i]['lastBuiltRevision']['SHA1']
                _url = bi[i]['remoteUrls'][0]
                git_url = 'https://devgit.cloudpassage.com/' + \
                        _url[_url.find('devgit')+7:].replace('_','/')
                git_branch = bi[i]['lastBuiltRevision']['branch'][0]['name']
                incoming_request['git'] = {}
                incoming_request['git']['commit'] = git_commit_id
                incoming_request['git']['url'] = git_url
                incoming_request['git']['branch'] = git_branch
                break

        self.broadcast(self.format_notification(incoming_request, True), True)
        return

    @botcmd
    def jenkins_list(self, mess, args):
        """List all jobs, optionally filter them using a search term."""
        self.connect_to_jenkins(mess.frm.channelname)
        return self.format_jobs([job for job in self.jenkins.get_jobs(folder_depth=None)
            if args.lower() in job['fullname'].lower()])

    @botcmd
    def jenkins_running(self, mess, args):
        """List all running jobs."""
        self.connect_to_jenkins(mess.frm.channelname)

        jobs = [job for job in self.jenkins.get_jobs()
                if 'anime' in job['color']]
        return self.format_running_jobs(jobs)

    @botcmd(split_args_with=None)
    def jenkins_param(self, mess, args):
        """List Parameters for a given job."""
        if len(args) == 0:
            return 'What Job would you like the parameters for?'

        self.connect_to_jenkins(mess.frm.channelname)

        job = self.jenkins.get_job_info(args[0])
        if job['actions'][1] != {}:
            job_param = job['actions'][1]['parameterDefinitions']
        elif job['actions'][0] != {}:
            job_param = job['actions'][0]['parameterDefinitions']
        else:
            job_param = []

        return self.format_params(job_param)

    @botcmd(split_args_with=None)
    def jenkins_output(self, mess, args):
        """Fetch latest jenkins buid output for a job."""
        if len(args) == 0:  # No Job name
            return 'What job output would you like?'

        self.connect_to_jenkins(mess.frm.channelname)
        job_name = args[0]

        job = self.jenkins.get_job_info(job_name)
        if job['builds']:
            last_run_number = job['builds'][0]['number']
        else:
            return 'job has not been build yet!'

        try:
            console_output = self.jenkins.get_build_console_output(job_name, last_run_number)
        except:
            return 'could not connect to jenkins'
        else:
            stream = io.BytesIO(console_output.encode())

        yield 'Fetching job output....'
        self.send_stream_request(mess.frm, stream, '{0} build #{1} output'.format(job_name, last_run_number))

    @botcmd(split_args_with=None)
    def jenkins_branch(self, mess, args):
        """ set a job git branch/commit id"""
        if len(args) < 2:  # No Job name or branch
            return 'missing job name or branch'

        self.connect_to_jenkins(mess.frm.channelname)
        job_name = args[0]
        branch = args[1]
        if not self.jenkins.job_exists(job_name):
            return 'job name is invalid'

        job_xml = self.jenkins.get_job_config(job_name)
        tree = et.fromstring(job_xml)
        tree.find('.//hudson.plugins.git.BranchSpec/name').text = branch
        new_job_xml = et.tostring(tree, encoding='utf-8')
        try:
            self.jenkins.reconfig_job(job_name, new_job_xml.decode('utf-8'))
        except:
            return 'failed to change the job build branch'

        return job_name + ' branch was successfully changed to ' + branch

    @botcmd(split_args_with=None)
    def jenkins_build(self, mess, args):
        """Build a Jenkins Job with the given parameters
        Example: !jenkins build test_project FOO:bar
        """
        if len(args) == 0:  # No Job name
            return 'What job would you like to build?'

        self.connect_to_jenkins(mess.frm.channelname)
        params = self.build_parameters(args[1:])

        # Is it a parameterized job ?
        job = self.jenkins.get_job_info(args[0])
        if job['actions'][0] == {} and job['actions'][1] == {}:
            self.jenkins.build_job(args[0])
        else:
            self.jenkins.build_job(args[0], params)

        running_job = self.search_job(args[0])
        return 'Your job should begin shortly: {0}'.format(
            self.format_jobs(running_job))

    @botcmd(split_args_with=None)
    def build(self, mess, args):
        """Shortcut for jenkins_build"""
        return self.jenkins_build(mess, args)

    @botcmd(split_args_with=None)
    def deploy(self, mess, args):
        """Shortcut for jenkins_build"""
        return self.jenkins_build(mess, args)

    @botcmd(split_args_with=None)
    def jenkins_deploy(self, mess, args):
        """Shortcut for jenkins_build"""
        return self.jenkins_build(mess, args)

    @botcmd
    def jenkins_unqueue(self, msg, args):
        """Cancel a queued job.
        Example !jenkins unqueue foo
        """
        self.connect_to_jenkins(mess.frm.channelname)

        try:
            queue = self.jenkins.get_queue_info()

            job = next((job for job in queue if job['task']['name'].lower() == args.lower()), None)

            if job:
                self.jenkins.cancel_queue(job['id'])
                return 'Unqueued job {0}'.format(job['task']['name'])
            else:
                return 'Could not find job {0}, but found the following: {1}'.format(
                    args, ', '.join(job['task']['name'] for job in queue))
        except JenkinsException as e:
            return 'Oops, {0}'.format(e)

    @botcmd(split_args_with=None)
    def jenkins_createjob(self, mess, args):
        """Create a Jenkins Job.
        Example: !jenkins createjob pipeline foo git@github.com:foo/bar.git
        """
        if len(args) < 2:  # No Job type or name
            return 'Oops, I need a type and a name for your new job.'

        if args[0] not in ('pipeline', 'multibranch'):
            return 'I\'m sorry, I can only create `pipeline` and \
                    `multibranch` jobs.'

        self.connect_to_jenkins(mess.frm.channelname)

        try:
            if args[0] == 'pipeline':
                self.jenkins.create_job(
                    args[1],
                    JENKINS_JOB_TEMPLATE_PIPELINE.format(repository=args[2]))

            elif args[0] == 'multibranch':
                repository = args[2].rsplit('/', maxsplit=2)[-2:]

                self.jenkins.create_job(
                    args[1],
                    JENKINS_JOB_TEMPLATE_MULTIBRANCH.format(
                        repo_owner=repository[0].split(':')[-1],
                        repo_name=repository[1].strip('.git')))
        except JenkinsException as e:
            return 'Oops, {0}'.format(e)
        return 'Your job has been created: {0}/job/{1}'.format(
            self.config['URL'], args[1])

    @botcmd(split_args_with=None)
    def jenkins_deletejob(self, mess, args):
        """Delete a Jenkins Job.
        Example: !jenkins deletejob foo
        """
        if len(args) < 1:  # No job name
            return 'Oops, I need the name of the job you want me to delete.'

        self.connect_to_jenkins(mess.frm.channelname)

        try:
            self.jenkins.delete_job(args[0])
        except JenkinsException as e:
            return 'Oops, {0}'.format(e)

        return 'Your job has been deleted.'

    @botcmd(split_args_with=None)
    def jenkins_enablejob(self, mess, args):
        """Enable a Jenkins Job.
        Example: !jenkins enablejob foo
        """
        if len(args) < 1:  # No job name
            return 'Oops, I need the name of the job you want me to enable.'

        self.connect_to_jenkins(mess.frm.channelname)

        try:
            self.jenkins.enable_job(args[0])
        except JenkinsException as e:
            return 'Oops, {0}'.format(e)

        return 'Your job has been enabled.'

    @botcmd(split_args_with=None)
    def jenkins_disablejob(self, mess, args):
        """Disable a Jenkins Job.
        Example: !jenkins disablejob foo
        """
        if len(args) < 1:  # No job name
            return 'Oops, I need the name of the job you want me to disable.'

        self.connect_to_jenkins(mess.frm.channelname)

        try:
            self.jenkins.disable_job(args[0])
        except JenkinsException as e:
            return 'Oops, {0}'.format(e)

        return 'Your job has been disabled.'

    @botcmd(split_args_with=None)
    def jenkins_createnode(self, mess, args):
        """Create a Jenkins Node with a JNLP Launcher with optionnal labels.
        Example: !jenkins createnode runner-foo-laptop /home/foo # without labels
        Example: !jenkins createnode runner-bar-laptop /home/bar linux docker # with labels
        """
        if len(args) < 1:  # No node name
            return 'Oops, I need a name and a working dir for your new node.'

        self.connect_to_jenkins(mess.frm.channelname)

        try:
            self.jenkins.create_node(
                name=args[0],
                remoteFS=args[1],
                labels=' '.join(args[2:]),
                exclusive=True,
                launcher=LAUNCHER_JNLP)
        except JenkinsException as e:
            return 'Oops, {0}'.format(e)

        return 'Your node has been created: {0}/computer/{1}'.format(
            self.config['URL'], args[0])

    @botcmd(split_args_with=None)
    def jenkins_deletenode(self, mess, args):
        """Delete a Jenkins Node.
        Example: !jenkins deletenode runner-foo-laptop
        """
        if len(args) < 1:  # No node name
            return 'Oops, I need the name of the node you want me to delete.'

        self.connect_to_jenkins(mess.frm.channelname)

        try:
            self.jenkins.delete_node(args[0])
        except JenkinsException as e:
            return 'Oops, {0}'.format(e)

        return 'Your node has been deleted.'

    @botcmd(split_args_with=None)
    def jenkins_enablenode(self, mess, args):
        """Enable a Jenkins Node.
        Example: !jenkins enablenode runner-foo-laptop
        """
        if len(args) < 1:  # No node name
            return 'Oops, I need the name of the node you want me to enable.'

        self.connect_to_jenkins(mess.frm.channelname)

        try:
            self.jenkins.enable_node(args[0])
        except JenkinsException as e:
            return 'Oops, {0}'.format(e)

        return 'Your node has been enabled.'

    @botcmd(split_args_with=None)
    def jenkins_disablenode(self, mess, args):
        """Disable a Jenkins Node.
        Example: !jenkins disablenode runner-foo-laptop
        """
        if len(args) < 1:  # No node name
            return 'Oops, I need the name of the node you want me to disable.'

        self.connect_to_jenkins(mess.frm.channelname)

        try:
            self.jenkins.disable_node(args[0])
        except JenkinsException as e:
            return 'Oops, {0}'.format(e)

        return 'Your node has been disabled.'

    def search_job(self, search_term):
        self.log.debug('Querying Jenkins for job "{0}"'.format(search_term))
        return [job for job in self.jenkins.get_jobs(folder_depth=None)
                if search_term.lower() == job['fullname'].lower()]

    def format_running_jobs(self, jobs):
        if len(jobs) == 0:
            return 'No running jobs.'

        jobs_info = [self.jenkins.get_job_info(job['name']) for job in jobs]
        return '\n\n'.join(['%s (%s)\n%s' % (
            job['name'],
            job['lastBuild']['url'],
            job['healthReport'][0]['description'])
                            for job in jobs_info]).strip()

    @staticmethod
    def format_jobs(jobs):
        if len(jobs) == 0:
            return 'No jobs found.'

        max_length = max([len(job['fullname']) for job in jobs])
        return '\n'.join(
            ['%s (%s)' % (job['fullname'].ljust(max_length), job['url'])
             for job in jobs]).strip()

    @staticmethod
    def format_params(job):
        """Format job parameters."""
        if len(job) == 0:
            return 'This job is not parameterized.'
        PARAM_TEMPLATE = Template("""{% for p in params %}Type: {{p.type}}
Description: {{p.description}}
Default Value: {{p.defaultParameterValue.value}}
Parameter Name: {{p.name}}

{% endfor %}""")
        return PARAM_TEMPLATE.render({'params': job})

    @staticmethod
    def format_notification(body, use_card):
        body['fullname'] = body.get('fullname', body['name'])
        if use_card:
            COLOR = {
                'default': 'yellow',
                'FAILURE': 'red',
                'SUCCESS': 'green'
            }
            build = body['build']
            git = body.get('git')
            branch = ', ' + git['branch'] if git else ''
            card = {
                'title': git.get('commit', '')[0:6] if git else '',
                'body': build['phase'] + " " + body['fullname'] + branch + " #" + str(build['number']),
                'link': git['url'] + '/commit/' + git['commit'] if git else '',
                'color': COLOR[build.get('status', 'default')],
            }
            return card
        else:
            NOTIFICATION_TEMPLATE = Template("""Build #{{build.number}} \
{{build.phase}} {{build.status}} for Job {{fullname}} ({{build.full_url}})
{% if build.scm %}Based on {{build.scm.url}}/commit/{{build.scm.commit}} \
({{build.scm.branch}}){% endif %} \
{% if git %}Based on {{git.url}}/commit/{{git.commit}} \
({{git.branch}}){% endif %}""")
            return NOTIFICATION_TEMPLATE.render(body)

    @staticmethod
    def build_parameters(params):
        if len(params) > 0:
            return {param.split(':')[0]: param.split(':')[1]
                    for param in params}
        return {'': ''}
