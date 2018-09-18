# -*- coding: utf-8 -*-

from mr.developer import common
from mr.developer.compat import b, s
import os
import subprocess
import re
import sys


logger = common.logger


class GitError(common.WCError):
    pass


class GitWorkingCopy(common.BaseWorkingCopy):
    """The git working copy.

    Now supports git 1.5 and 1.6+ in a single codebase.
    """

    # TODO: make this configurable? It might not make sense however, as we
    # should make master and a lot of other conventional stuff configurable
    _upstream_name = "origin"

    def __init__(self, source):
        self.git_executable = common.which('git')
        if 'rev' in source and 'revision' in source:
            raise ValueError("The source definition of '%s' contains "
                             "duplicate revision options." % source['name'])
        # 'rev' is canonical
        if 'revision' in source:
            source['rev'] = source['revision']
            del source['revision']
        if 'branch' in source and 'rev' in source:
            logger.error("Cannot specify both branch (%s) and rev/revision "
                         "(%s) in source for %s",
                         source['branch'], source['rev'], source['name'])
            sys.exit(1)
        self._git_stdout = []
        super(GitWorkingCopy, self).__init__(source)

    def git_run(self, commands, **kwargs):
        commands.insert(0, self.git_executable)
        kwargs['stdout'] = subprocess.PIPE
        kwargs['stderr'] = subprocess.PIPE
        # This should ease things up when multiple processes are trying to send
        # back to the main one large chunks of output
        kwargs['bufsize'] = -1
        cmd = subprocess.Popen(commands, **kwargs)
        stdout, stderr = cmd.communicate()
        self._git_stdout.append(s(stdout))
        if cmd.returncode != 0:
            raise GitError("'%s'\n%s\n%s" % (
                ' '.join(commands), s(stdout), s(stderr)))
        return stdout

    def git_output(self):
        return "".join(self._git_stdout)

    @common.memoize
    def git_version(self):
        try:
            output = self.git_run(['--version'])
        except GitError as error:
            logger.error("Could not determine git version")
            logger.error(error.args[0])
            sys.exit(1)

        m = re.search(b("git version (\d+)\.(\d+)(\.\d+)?(\.\d+)?"), output)
        if m is None:
            logger.error("Unable to parse git version output")
            logger.error("'git --version' output was:\n%s" % (output))
            sys.exit(1)
        version = m.groups()

        if version[3] is not None:
            version = (
                int(version[0]),
                int(version[1]),
                int(version[2][1:]),
                int(version[3][1:]))
        elif version[2] is not None:
            version = (
                int(version[0]),
                int(version[1]),
                int(version[2][1:]))
        else:
            version = (int(version[0]), int(version[1]))
        if version < (1, 5):
            logger.error(
                "Git version %s is unsupported, please upgrade",
                ".".join([str(v) for v in version]))
            sys.exit(1)
        return version

    @property
    def _remote_branch_prefix(self):
        version = self.git_version()
        if version < (1, 6, 3):
            return self._upstream_name
        else:
            return 'remotes/%s' % self._upstream_name

    def new_feature(self, **kwargs):
        current = self.git_current_branch()
        preferred = self.source.get('preferred-branches')
        if current is None or preferred is None or current in preferred:
            logger.error('You are not on a feature branch.')
            return False
        if not len(preferred):
            logger.error('Did you already setup your branch?')
            return False
        name = self.source['name']
        path = self.source['path']
        if os.path.exists(path):
            self.git_run(["fetch", "--prune"], cwd=path)
        else:
            self.git_checkout(**kwargs)
        is_local, is_remote = self.git_branch_status(current)
        if not is_local:
            try:
                self.git_run(["branch", current, "%s/%s" % (self._remote_branch_prefix, preferred[0])], cwd=path)
                logger.info("Created branch '%s' for '%s'." % (current, name))
            except:
                logger.error("Could not create branch '%s' for '%s'." % (
                    current, name))
                raise
                return False
        if current != self.git_current_branch(cwd=path):
            try:
                self.git_run(["checkout", current], cwd=path)
            except GitError:
                logger.error("Could not checkout branch '%s' for '%s'." % (
                    current, name))
                return False
        if not is_remote:
            try:
                self.git_run(["push", "--set-upstream", self._upstream_name, current], cwd=path)
            except GitError:
                logger.error("Could not push branch '%s' for '%s'." % (
                    current, name))
                return False
            logger.info("Push branch '%s' for '%s'." % (current, name))
        return True

    def git_current_branch(self, **kwargs):
        try:
            output = self.git_run(["symbolic-ref", "--short", "HEAD"], **kwargs)
        except GitError:
            return None
        return s(output).strip()

    def git_branch_status(self, branch):
        output = self.git_run(["branch", "-a"], cwd=self.source['path'])
        is_local = False
        is_remote = False
        if re.search(b("^(\*| ) %s$" % re.escape(branch)), output, re.M):
            is_local = True
        if re.search(b("^  " + re.escape(self._remote_branch_prefix) + "\/" + re.escape(branch) + "$"), output, re.M):
            is_remote = True
        return (is_local, is_remote)

    def auto_select_branch(self):
        desired = self.source.get('branch')
        preferred = self.source.get('preferred-branches')
        if desired is not None or preferred is None:
            return
        current = self.git_current_branch()
        if current is None:
            return
        if current not in preferred and len(preferred):
            current = preferred[0]
        self.output((logger.info, "Auto-selecting branch %s" % current))
        self.source['branch'] = current

    def git_merge_rbranch(self, accept_missing=False):
        branch = self.source.get('branch', 'master')
        is_local, is_remote = self.git_branch_status(branch)
        if not is_local or not is_remote:
            if accept_missing:
                logger.info("No such branch %r", branch)
                return
            else:
                logger.error("No such branch %r", branch)
                sys.exit(1)

        self.git_run(["merge", "%s/%s" % (self._remote_branch_prefix, branch)], cwd=self.source['path'])

    def git_checkout(self, **kwargs):
        name = self.source['name']
        path = self.source['path']
        url = self.source['url']
        if os.path.exists(path):
            self.output((logger.info, "Skipped cloning of existing package '%s'." % name))
            return
        msg = "Cloned '%s' with git" % name
        if "branch" in self.source:
            msg += " using branch '%s'" % self.source['branch']
        msg += " from '%s'." % url
        self.output((logger.info, msg))
        args = ["clone", "--quiet"]
        if 'depth' in self.source:
            args.extend(["--depth", self.source["depth"]])
        if "branch" in self.source:
            args.extend(["-b", self.source["branch"]])
        args.extend([url, path])
        self.git_run(args)
        if 'rev' in self.source:
            self.git_switch_branch()
        if 'pushurl' in self.source:
            self.git_set_pushurl()

        update_git_submodules = self.source.get('submodules', kwargs['submodules'])
        if update_git_submodules in ['always', 'checkout']:
            initialized = self.git_init_submodules()
            # Update only new submodules that we just registered. this is for safety reasons
            # as git submodule update on modified submodules may cause code loss
            for submodule in initialized:
                self.git_update_submodules(submodule=submodule)
                self.output((logger.info, "Initialized '%s' submodule at '%s' with git." % (name, submodule)))

        if kwargs.get('verbose', False):
            return self.git_output()

    def git_switch_branch(self, accept_missing=False):
        """Switch branches.

        If accept_missing is True, we do not switch the branch if it
        is not there.  Useful for switching back to master.
        """
        path = self.source['path']
        branch = self.source.get('branch', 'master')
        is_local, is_remote = self.git_branch_status(branch)
        if 'rev' in self.source:
            # A tag or revision was specified instead of a branch
            argv = ["checkout", self.source['rev']]
            self.output((logger.info, "Switching to rev '%s'." % self.source['rev']))
        elif is_local:
            # the branch is local, normal checkout will work
            argv = ["checkout", branch]
            self.output((logger.info, "Switching to branch '%s'." % branch))
        elif is_remote:
            # the branch is not local, normal checkout won't work here
            rbranch = "%s/%s" % (self._remote_branch_prefix, branch)
            argv = ["checkout", "-b", branch, rbranch]
            self.output((logger.info, "Switching to remote branch '%s'." % rbranch))
        elif accept_missing:
            self.output((logger.info, "No such branch %r", branch))
            return
        else:
            raise GitError("No such branch {}".format(branch))
        # runs the checkout with predetermined arguments
        self.git_run(argv, cwd=path)

    def git_update(self, **kwargs):
        name = self.source['name']
        path = self.source['path']
        self.output((logger.info, "Updated '%s' with git." % name))
        # First we fetch. This should always be possible.
        self.git_run(["fetch", "--prune"], cwd=path)
        if 'rev' in self.source:
            self.git_switch_branch()
        elif 'branch' in self.source:
            self.git_switch_branch()
            self.git_merge_rbranch()
        else:
            # We may have specified a branch previously but not
            # anymore.  In that case, we want to revert to master.
            self.git_switch_branch(accept_missing=True)
            self.git_merge_rbranch(accept_missing=True)

        update_git_submodules = self.source.get('submodules', kwargs['submodules'])
        if update_git_submodules in ['always']:
            initialized = self.git_init_submodules()
            # Update only new submodules that we just registered. this is for safety reasons
            # as git submodule update on modified subomdules may cause code loss
            for submodule in initialized:
                self.git_update_submodules(submodule=submodule)
                self.output((logger.info, "Initialized '%s' submodule at '%s' with git." % (name, submodule)))

        if kwargs.get('verbose', False):
            return self.git_output()

    def checkout(self, **kwargs):
        name = self.source['name']
        path = self.source['path']
        self.auto_select_branch()
        update = self.should_update(**kwargs)
        if os.path.exists(path):
            if update:
                return self.update(**kwargs)
            elif self.matches():
                self.output((logger.info, "Skipped checkout of existing package '%s'." % name))
            else:
                self.output((logger.warning, "Checkout URL for existing package '%s' differs. Expected '%s'." % (name, self.source['url'])))
        else:
            return self.git_checkout(**kwargs)

    def status(self, **kwargs):
        path = self.source['path']
        output = self.git_run(["status", "-s", "-b"], cwd=path)
        lines = output.strip().split(b('\n'))
        if len(lines) == 1:
            if b('ahead') in lines[0]:
                status = 'ahead'
            else:
                status = 'clean'
        else:
            status = 'dirty'
        if kwargs.get('verbose', False):
            return status, self.git_output()
        else:
            return status

    def matches(self):
        name = self.source['name']
        path = self.source['path']
        # This is the old matching code: it does not work on 1.5 due to the
        # lack of the -v switch
        output = self.git_run(["remote", "show", "-n", self._upstream_name], cwd=path)
        return (self.source['url'] in s(output).split())

    def update(self, **kwargs):
        name = self.source['name']
        if not self.matches():
            self.output((logger.warning, "Can't update package '%s' because its URL doesn't match." % name))
        if self.status() != 'clean' and not kwargs.get('force', False):
            raise GitError("Can't update package '%s' because it's dirty." % name)
        self.auto_select_branch()
        return self.git_update(**kwargs)

    def git_set_pushurl(self):
        self.git_run(
            ["config", "remote.%s.pushurl" % self._upstream_name, self.source['pushurl']],
            cwd=self.source['path'])

    def git_init_submodules(self):
        output = self.git_run(['submodule', 'init'], cwd=self.source['path'])
        return re.findall(
            r'\s+[\'"](.*?)[\'"]\s+\(.+\)',
            s(output))

    def git_update_submodules(self, submodule='all'):
        argv = ['submodule', 'update']
        if submodule != 'all':
            argv.append(submodule)
        self.git_run(arv, cwd=self.source['path'])
