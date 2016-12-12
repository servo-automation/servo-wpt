from api_provider import ServoGithubAPIProvider
from datetime import datetime

import json, os, subprocess, shutil, sys

RR_PATH = os.path.expanduser('~/.local/share/rr')
TEMP_LOG = '/tmp/wpt_log'
WPT_COMMAND = './mach test-%s --debugger=rr --debugger-args=record --no-pause --log-raw %s'
OUTPUT_HEAD = 'Tests with unexpected results:'
SUBTEST_PREFIX = 'Unexpected subtest result'
TEST_DEFAULT = {'record': None, 'issue': None, 'subtest': {}, 'notified': False}
NOTIFICATION = ('Hey! I have a `rr` recording corresponding to this failure.'
                'Feel free to ping @jdm in case you need it!')


class IntermittentWatcher(object):
    def __init__(self, upstream, user, token, db, build='debug', log_path=None):
        os.chdir(upstream)
        self.api = ServoGithubAPIProvider(user, token)
        sys.path.append(os.path.join(db))
        from db import IntermittentsDB
        with open(os.path.join(db, 'intermittents.json'), 'r') as fd:
            self.db = IntermittentsDB(json.load(fd))
        self.last_updated = datetime.now().day - 1
        self.build = 'debug'
        if build == 'release':
            self.build = 'release'
        self.test = 'wpt'
        self.log_path = log_path
        if log_path:
            with open(log_path, 'r') as fd:
                self.results = json.load(fd)

    def execute(self, command, call= lambda l: None):
        out = ''
        print '\033[93m%s\033[0m' % command
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        for line in iter(proc.stdout.readline, ''):
            sys.stdout.write(line)
            sys.stdout.flush()
            call(line)      # call a custom function with stdout lines as and whenever we get one
            out += line
        proc.wait()
        return out

    def log(self, msg):
        print '\033[91m%s\033[0m: \033[92m%s\033[0m' % (datetime.now(), msg)

    def run(self):
        current = {}
        self.log('Running tests...')
        command = WPT_COMMAND % (self.test, TEMP_LOG)
        if self.build == 'release':
            command += ' --release'
        out = self.execute(command)
        out = out[(out.find(OUTPUT_HEAD) + len(OUTPUT_HEAD)):-1].strip()

        with open(TEMP_LOG, 'r') as fd:
            for line in fd:
                obj = json.loads(line)
                if obj['thread'] == 'MainThread':
                    continue
                if obj['action'] == 'test_start':
                    current[obj['thread']] = obj['test']    # tests running in each thread
                    self.results.setdefault(obj['test'], TEST_DEFAULT)
                elif obj['action'] == 'process_output':
                    test = current[obj['thread']]
                    if obj['data'].startswith('rr: Saving'):    # get the rr-record location
                        data = obj['data'].split()
                        old = self.results[test]['record']
                        new = data[-1][1:-2]
                        if old:
                            self.log('Replacing existing record %r with new one %r for test %r' % \
                                     (old, new, test))
                        self.results[test]['record'] = new
                if obj.get('expected'):     # there's an unexpected result
                    test, subtest = current[obj['thread']], obj.get('subtest', obj['test'])
                    issues = self.db.query(test)
                    if issues:      # FIXME: handle multiple results?
                        self.results[test]['issue'] = issues[0]['number']
                    self.results[test]['subtest'][subtest] = {'data': '', 'status': obj['status']}

        for result in map(str.strip, out.split('\n\n')):
            data = result.splitlines()
            name = data[0][data[0].find('/'):]
            if SUBTEST_PREFIX in result:
                test = name[:-1]    # strip out the colon
                subtest = data[1][(data[1].find(']') + 2):]     # get the subtest name
            else:
                test, subtest = name, name      # tests without subtests
            self.results[test]['subtest'][subtest]['data'] = result

        self.log('Cleaning up recordings...')
        for test, result in self.results.iteritems():
            if result['subtest']:
                if not result['notified']:
                    if result['issue']:
                        self.log('Posting comment...')
                    else:
                        self.log('Opening issue...')
            else:
                result = self.results.pop(test)
                if result['record']:
                    self.log('Removing unused record %r for test %r' % (result['record'], test))
                    shutil.rmtree(result['record'])

        with open(self.log_path, 'w') as fd:
            self.log('Dumping the test results...')
            json.dump(self.results, fd)

    def create_issue(self, test):
        subtests = self.results[test]['subtest'].values()
        status = subtests[0]['status']
        title = 'Intermittent %s in %s' % (status.lower(), self.results[test])
        body = '\n\n'.join(map(lambda r: '    ' + r['data'], subtests) + [NOTIFICATION])
        labels = ['I-intermittent']
        if self.test == 'css':
            labels.append('A-content/css')
        self.api.create_issue(title, body, labels)

    def post_comment(self, test):
        subtests = self.results[test]['subtest'].values()
        body = '\n\n'.join(map(lambda r: '    ' + r['data'], subtests) + [NOTIFICATION])
        self.api.post_comment(body, self.results[test]['issue'])

    def update(self):
        self.log('Updating upstream...')
        self.execute('git pull upstream master')
        self.log('Building in %s mode...' % self.build)
        self.execute('./mach build --%s' % self.build)

    def start(self):
        while True:
            cur_time = datetime.now()
            if cur_time.hour >= 0 and cur_time.day > self.last_updated:
                self.update()
                self.last_updated = cur_time.day
            self.run()
            self.test = 'wpt' if self.test == 'css' else 'css'
