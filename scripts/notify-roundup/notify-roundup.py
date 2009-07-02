#!/usr/bin/python
#
# notify-roundup.py: call into a roundup tracker to notify it of commits
#
# USAGE: notify-roundup.py TRACKER-HOME REPOS-DIR REVISION
#        notify-roundup.py TRACKER-HOME REPOS-DIR REVISION AUTHOR PROPNAME
#
#   TRACKER-HOME is the tracker to notify
#
# See end of file for change history

import sys, os, time, cStringIO, re, logging, smtplib, ConfigParser, socket


# configure logging
logger = logging.getLogger('notify-roundup')
hdlr = logging.FileHandler('/tmp/log')
formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
hdlr.setFormatter(formatter)
logger.addHandler(hdlr)
logger.propogate = False
logger.setLevel(logging.DEBUG)

#print sys.argv
# now try to import stuff that might not work
try:
    import roundup.instance, roundup.date

    import svn.fs
    import svn.delta
    import svn.repos
    import svn.core
except:
    logger.exception('Exception while importing Roundup and SVN')
    sys.exit(1)

class Failed(Exception):
    pass
class Unauthorised(Failed):
    pass

def main(pool):
    '''Handle the commit revision.
    '''
    # command-line args
    cfg = ConfigParser.ConfigParser()
    cfg.read(sys.argv[1])
    repos_dir = sys.argv[2]
    revision = int(sys.argv[3])

    # get a handle on the revision in the repository
    repos = Repository(repos_dir, revision, pool)

    repos.klass = cfg.get('main', 'item-class')
    if not repos.extract_info():
        return

    if cfg.has_option('main', 'host'):
        repos.host = cfg.get('main', 'host')
    else:
        repos.host = socket.gethostname()

    mode = cfg.get('main', 'mode')
    if mode == 'local':
        notify_local(cfg.get('local', 'tracker-home'), repos)
    elif mode == 'email':
        tracker_address = cfg.get('email', 'tracker-address')
        domain = cfg.get('email', 'default-domain')
        smtp_host = cfg.get('email', 'smtp-host')
        if cfg.has_option('address mappings', repos.author):
            mapped_email = cfg.get('address mappings', repos.author)
        elif cfg.has_option('address mappings', '*'):
            mapped_email = cfg.get('address mappings', '*')
        else:
            mapped_email = repos.author
        if '@' not in mapped_email:
            mapped_email += domain
        notify_email(tracker_address, mapped_email, smtp_host, repos)
    else:
        logging.error('invalid mode %s in config file'%mode)


def notify_email(tracker_address, from_address, smtp_host, repos):
    subject = '[%s%s] SVN commit message'%(repos.klass, repos.itemid)
    if repos.status:
        subject += ' [status=%s]'%repos.status
    date = time.strftime('%Y-%m-%d %H:%M:%S', repos.date)
    message = '''From: %s
To: %s
Subject: %s

revision=%s
host=%s
repos=%s
date=%s
summary=%s

%s'''%(from_address, tracker_address, subject, repos.rev, repos.host,
    repos.repos_dir, date, repos.summary, repos.message)

    logger.debug('MESSAGE TO SEND\n%s'%message)

    smtp = smtplib.SMTP(smtp_host)
    try:
        smtp.sendmail(from_address, [tracker_address], message)
    except:
        logging.exception('mail to %r from %r via %r'%(tracker_address,
            from_address, smtp_host))

def notify_local(tracker_home, repos):
    # get a handle on the tracker db
    tracker = roundup.instance.open(tracker_home)
    db = tracker.open('admin')
    try:
        notify_local_inner(db, tracker_home, repos)
    except:
        db.rollback()
        db.close()
        raise

def notify_local_inner(db, tracker_home, repos):
    # sanity check
    try:
        db.getclass(repos.klass)
    except KeyError:
        logger.error('no such tracker class %s'%repos.klass)
        raise Failed
    if not db.getclass(repos.klass).hasnode(repos.itemid):
        logger.error('no such %s item %s'%(repos.klass, repos.itemid))
        raise Failed
    if repos.status:
        try:
            status_id = db.status.lookup(repos.status)
        except KeyError:
            logger.error('no such status %s'%repos.status)
            raise Failed

    print repos.host, repos.repos_dir
    # get the svn repo information from the tracker
    try:
        svn_repo_id = db.svn_repo.stringFind(host=repos.host,
            path=repos.repos_dir)[0]
    except IndexError:
        logger.error('no repository %s in tracker'%repos.repos_dir)
        raise Failed

    # log in as the appropriate user
    try:
        matches = db.user.stringFind(svn_name=repos.author)
    except KeyError:
        # the user class has no property "svn_name"
        matches = []
    if matches:
        userid = matches[0]
    else:
        try:
            userid = db.user.lookup(repos.author)
        except KeyError:
            raise Failed, 'no Roundup user matching %s'%repos.author
    username = db.user.get(userid, 'username')
    db.close()

    # tell Roundup
    tracker = roundup.instance.open(tracker_home)
    db = tracker.open(username)

    # check perms
    if not db.security.hasPermission('Create', userid, 'svn_rev'):
        raise Unauthorised, "Can't create items of class 'svn_rev'"
    if not db.security.hasPermission('Create', userid, 'msg'):
        raise Unauthorised, "Can't create items of class 'msg'"
    if not db.security.hasPermission('Edit', userid, repos.klass,
            'messages', repos.itemid):
        raise Unauthorised, "Can't edit items of class '%s'"%repos.klass
    if repos.status and not db.security.hasPermission('Edit', userid,
            repos.klass, 'status', repos.itemid):
        raise Unauthorised, "Can't edit items of class '%s'"%repos.klass

    # create the revision
    svn_rev_id = db.svn_rev.create(repository=svn_repo_id, revision=repos.rev)

    # add the message to the spool
    date = roundup.date.Date(repos.date)
    msgid = db.msg.create(content=repos.message, summary=repos.summary,
        author=userid, date=date, revision=svn_rev_id)
    klass = db.getclass(repos.klass)
    messages = klass.get(repos.itemid, 'messages')
    messages.append(msgid)
    klass.set(repos.itemid, messages=messages)
    
    # and set the status
    if repos.status:
        klass.set(repos.itemid, status=status_id)

    db.commit()
    logger.debug('Roundup modification complete')
    db.close()


def _select_adds(change):
  return change.added
def _select_deletes(change):
  return change.path is None
def _select_modifies(change):
  return not change.added and change.path is not None


def generate_list(output, header, changelist, selection):
    items = [ ]
    for path, change in changelist:
      if selection(change):
        items.append((path, change))
    if not items:
      return

    output.write('%s:\n' % header)
    for fname, change in items:
      if change.item_kind == svn.core.svn_node_dir:
        is_dir = '/'
      else:
        is_dir = ''
      if change.prop_changes:
        if change.text_changed:
          props = '   (contents, props changed)'
        else:
          props = '   (props changed)'
      else:
        props = ''
      output.write('   %s%s%s\n' % (fname, is_dir, props))
      if change.added and change.base_path:
        if is_dir:
          text = ''
        elif change.text_changed:
          text = ', changed'
        else:
          text = ' unchanged'
        output.write('      - copied%s from r%d, %s%s\n'
                     % (text, change.base_rev, change.base_path[1:], is_dir))

class Repository:
    '''Hold roots and other information about the repository. From mailer.py
    '''
    def __init__(self, repos_dir, rev, pool):
        self.repos_dir = repos_dir
        self.rev = rev
        self.pool = pool

        self.repos_ptr = svn.repos.svn_repos_open(repos_dir, pool)
        self.fs_ptr = svn.repos.svn_repos_fs(self.repos_ptr)

        self.roots = {}

        self.root_this = self.roots[rev] = svn.fs.revision_root(self.fs_ptr,
            rev, self.pool)

        self.author = self.get_rev_prop(svn.core.SVN_PROP_REVISION_AUTHOR)

    def get_rev_prop(self, propname):
        return svn.fs.revision_prop(self.fs_ptr, self.rev, propname, self.pool)

    def extract_info(self):
        issue_re = re.compile('^\s*(%s)\s*(\d+)(\s+(\S+))?\s*$'%self.klass,
            re.I)

        # parse for Roundup item information
        log = self.get_rev_prop(svn.core.SVN_PROP_REVISION_LOG) or ''
        for line in log.splitlines():
            m = issue_re.match(line)
            if m:
                break
        else:
            # nothing to do
            return

        # parse out the issue information
        klass = m.group(1)
        self.itemid = m.group(2)

        issue = klass + self.itemid
        self.status = m.group(4)

        logger.debug('Roundup info item=%r, status=%r'%(issue, self.status))

        # get all the changes and sort by path
        editor = svn.repos.RevisionChangeCollector(self.fs_ptr, self.rev,
            self.pool)
        e_ptr, e_baton = svn.delta.make_editor(editor, self.pool)
        svn.repos.svn_repos_replay(self.root_this, e_ptr, e_baton, self.pool)

        changelist = editor.changes.items()
        changelist.sort()

        # figure out the changed directories
        dirs = { }
        for path, change in changelist:
            if change.item_kind == svn.core.svn_node_dir:
                dirs[path] = None
            else:
                idx = path.rfind('/')
                if idx == -1:
                    dirs[''] = None
                else:
                    dirs[path[:idx]] = None

        dirlist = dirs.keys()

        # figure out the common portion of all the dirs. note that there is
        # no "common" if only a single dir was changed, or the root was changed.
        if len(dirs) == 1 or dirs.has_key(''):
            commondir = ''
        else:
            common = dirlist.pop().split('/')
            for d in dirlist:
                parts = d.split('/')
                for i in range(len(common)):
                    if i == len(parts) or common[i] != parts[i]:
                        del common[i:]
                        break
            commondir = '/'.join(common)
            if commondir:
                # strip the common portion from each directory
                l = len(commondir) + 1
                dirlist = [ ]
                for d in dirs.keys():
                    if d == commondir:
                        dirlist.append('.')
                    else:
                        dirlist.append(d[l:])
            else:
                # nothing in common, so reset the list of directories
                dirlist = dirs.keys()

        # compose the basic subject line. later, we can prefix it.
        dirlist.sort()
        dirlist = ' '.join(dirlist)

        if commondir:
            self.summary = 'r%d - in %s: %s' % (self.rev, commondir, dirlist)
        else:
            self.summary = 'r%d - %s' % (self.rev, dirlist)

        # Generate email for the various groups and option-params.
        output = cStringIO.StringIO()

        # print summary sections
        generate_list(output, 'Added', changelist, _select_adds)
        generate_list(output, 'Removed', changelist, _select_deletes)
        generate_list(output, 'Modified', changelist, _select_modifies)

        output.write('Log:\n%s\n'%log)

        self.message = output.getvalue()

        svndate = self.get_rev_prop(svn.core.SVN_PROP_REVISION_DATE)
        self.date = time.localtime(svn.core.secs_from_timestr(svndate,
            self.pool))

        return True

if __name__ == '__main__':
    try:
        svn.core.run_app(main)
    except Failed, message:
        logger.error(message)
        sys.exit(1)
    except:
        logger.exception('top level')
        sys.exit(1)

#
# 2005-05-16 - 1.2
# 
#   - Status wasn't being set by ID in local mode
#   - Wasn't catching errors in local changes, hence not cleaning up db
#     correctly
#   - svnauditor.py wasn't handling the fifth argument from notify-roundup.py
#   - viewcvs_url formatting wasn't quite right
#
# 2005-05-04 - 1.1
#   - Several fixes from  Ron Alford
#   - Don't change issue titles to "SVN commit message..."
# 
# 2005-04-26 - 1.0
#   - Initial version released
#
