
#
# For each openmdao subpackage, this script creates a releaseinfo.py file and 
# builds a source distribution.
#
import sys, os
import shutil
import logging
from subprocess import Popen, STDOUT, PIPE, check_call
from datetime import date
from optparse import OptionParser
import tempfile
import StringIO
import tarfile
import zipfile

# this module requires openmdao.devtools to be installed in the python environment

# get the list of openmdao subpackages from mkinstaller.py
from mkinstaller import openmdao_packages

relfile_template = """
# This file is automatically generated

__version__ = '%(version)s'
__comments__ = \"\"\"%(comments)s\"\"\"
__date__ = '%(date)s'
__commit__ = '%(commit)s'
"""

PRODUCTION_DISTS_URL = 'http://openmdao.org/dists'

def get_git_log_info(fmt):
    try:
        p = Popen('git log -1 --format=format:"%s"' % fmt, 
                  stdout=PIPE, stderr=STDOUT, env=os.environ, shell=True)
        out = p.communicate()[0]
        ret = p.returncode
    except:
        return ''
    else:
        return out.strip()

def get_branch():
    p = Popen('git branch', 
              stdout=PIPE, stderr=STDOUT, env=os.environ, shell=True)
    brlist = [b.strip() for b in p.communicate()[0].split('\n')]
    for b in brlist:
        if b.startswith('*'):
            return b[2:]
    return ''

def get_releaseinfo_str(version):
    """Creates the content of the releaseinfo.py files"""
    opts = {}
    f = StringIO.StringIO()
    opts['version'] = version
    opts['date'] = get_git_log_info("%ci")
    opts['comments'] = get_git_log_info("%b%+s%+N")
    opts['commit'] = get_git_log_info("%H")
    f.write(relfile_template % opts)
    return f.getvalue()

def create_releaseinfo_file(projname, relinfo_str):
    """Creates a releaseinfo.py file in the current directory"""
    dirs = projname.split('.')
    os.chdir(os.path.join(*dirs))
    print 'creating releaseinfo.py for %s' % projname
    with open('releaseinfo.py', 'w') as f:
        f.write(relinfo_str)
        
def rollback_releaseinfo_file(projname, relinfo_str):
    """Reverts a releaseinfo.py file back to the last commit"""
    dirs = projname.split('.')
    os.chdir(os.path.join(*dirs))
    print 'rolling back releaseinfo.py for %s' % projname
    os.system('git checkout -- releaseinfo.py')

def _has_checkouts():
    cmd = 'git status -s'
    p = Popen(cmd, stdout=PIPE, stderr=STDOUT, env=os.environ, shell=True)
    out = p.communicate()[0]
    ret = p.returncode
    if ret != 0:
        logging.error(out)
        raise RuntimeError(
             'error while getting status of git repository from directory %s (return code=%d): %s'
              % (os.getcwd(), ret, out))
    for line in out.split('\n'):
        line = line.strip()
        if len(line)>1 and not line.startswith('?'):
            return True
    return False

def _build_dist(build_type, destdir):
    cmd = '%s setup.py %s -d %s' % (sys.executable, build_type, destdir)
    p = Popen(cmd, stdout=PIPE, stderr=STDOUT, env=os.environ, shell=True)
    out = p.communicate()[0]
    ret = p.returncode
    if ret != 0:
        logging.error(out)
        raise RuntimeError(
             'error while building %s in %s (return code=%d): %s'
              % (build_type, os.getcwd(), ret, out))

def _build_sdist(projdir, destdir, version):
    """Build an sdist out of a develop egg."""
    startdir = os.getcwd()
    try:
        os.chdir(projdir)
        # clean up any old builds
        if os.path.exists('build'):
            shutil.rmtree('build')
        _build_dist('sdist', destdir)
        if os.path.exists('build'):
            shutil.rmtree('build', ignore_errors=True)
        if sys.platform.startswith('win'):
            os.chdir(destdir)
            # unzip the .zip file and tar it up so setuptools will find it on the server
            base = os.path.basename(projdir)+'-%s' % version
            zipname = base+'.zip'
            tarname = base+'.tar.gz'
            zarch = zipfile.ZipFile(zipname, 'r')
            zarch.extractall()
            zarch.close()
            archive = tarfile.open(tarname, 'w:gz')
            archive.add(base)
            archive.close()
            os.remove(zipname)
            shutil.rmtree(base)
    finally:
        os.chdir(startdir)

def _build_bdist_egg(projdir, destdir):
    startdir = os.getcwd()
    try:
        os.chdir(projdir)
        _build_dist('bdist_egg', destdir)
    finally:
        os.chdir(startdir)

def _find_top_dir():
    p = Popen('git rev-parse --show-toplevel', 
              stdout=PIPE, stderr=STDOUT, env=os.environ, shell=True)
    return p.communicate()[0].strip()

def main():
    """Create an OpenMDAO release, placing the following files in the 
    specified destination directory:
    
        - a tar file of the repository
        - source distribs of all of the openmdao subpackages
        - binary eggs for openmdao subpackages with compiled code
        - an installer script for the released version of openmdao that will
          create a virtualenv and populate it with all of the necessary
          dependencies needed to use openmdao
        
    The sphinx docs will also be built.
          
    In order to run this, you must be in a git repository that has not changed
    since the last commit, and in the process of running, a number of
    releaseinfo.py files will be updated with new version information and will
    be commited with the comment 'updated revision info and conf.py files'.
        
    """
    parser = OptionParser()
    parser.add_option("-d", "--destination", action="store", type="string", dest="destdir",
                      help="directory where distributions will be placed")
    parser.add_option("--disturl", action="store", type='string', dest="disturl",
                      default=PRODUCTION_DISTS_URL,
                      help="if not equal to the url for openmdao production distributions "
                           "(%s), "
                           "release will be a test release (no repo tag, commit not required)" % 
                           PRODUCTION_DISTS_URL)
    parser.add_option("--version", action="store", type="string", dest="version",
                      help="version string applied to all openmdao distributions")
    parser.add_option("-m", action="store", type="string", dest="comment",
                      help="optional comment for version tag")
    (options, args) = parser.parse_args(sys.argv[1:])
    
    if not options.version or not options.destdir:
        parser.print_help()
        sys.exit(-1)
        
    orig_branch = get_branch()
    if not orig_branch:
        print "no git branch found. aborting"
        sys.exit(-1)
    answer = raw_input('Your current branch is %s.  Is this correct? (Y/N) ' % orig_branch)
    answer = answer.lower().strip()
    if not answer in ['y', 'yes']:
        sys.exit(-1)
    
    has_checkouts = _has_checkouts()
    if has_checkouts:
        answer = raw_input('There are uncommitted changes. Do you want to continue with the release? (Y/N) ')
        answer = answer.lower().strip()
        if not answer in ['y', 'yes']:
            sys.exit(-1)
        print "stashing current state"
        os.system("git stash")
        
    relbranch = "release_%s" % options.version
    print "creating release branch"
    os.system("git branch %s" % relbranch)
    if has_checkouts:
        print "applying stash to release branch"
        os.system("git stash apply")
    
    destdir = os.path.realpath(options.destdir)
    if not os.path.exists(destdir):
        os.makedirs(destdir)

    releaseinfo_str = get_releaseinfo_str(options.version)
    startdir = os.getcwd()
    tarname = os.path.join(destdir,
                           'openmdao_src-%s.tar.gz' % options.version)
    
    topdir = _find_top_dir()
    
    try:
        for project_name, pdir, pkgtype in openmdao_packages:
            pdir = os.path.join(topdir, pdir, project_name)
            if 'src' in os.listdir(pdir):
                os.chdir(os.path.join(pdir, 'src'))
            else:
                os.chdir(pdir)
            create_releaseinfo_file(project_name, releaseinfo_str)

        # build the docs
        devtools_dir = os.path.join(topdir,'openmdao.devtools',
                                    'src','openmdao','devtools')
        check_call([sys.executable, os.path.join(devtools_dir,'build_docs.py'),
                    '-v', options.version])
        shutil.move(os.path.join(topdir,'docs','_build'), 
                    os.path.join(destdir,'_build'))
        if options.disturl == PRODUCTION_DISTS_URL:
            check_call(['git', 'commit', '-a', '-m', 
                        '"updating release info and sphinx config files for release %s"' % 
                        options.version])

        for project_name, pdir, pkgtype in openmdao_packages:
            pdir = os.path.join(topdir, pdir, project_name)
            if 'src' in os.listdir(pdir):
                os.chdir(os.path.join(pdir, 'src'))
            else:
                os.chdir(pdir)
            print 'building %s' % project_name
            _build_sdist(pdir, destdir, options.version)
            if pkgtype == 'bdist_egg':
                _build_bdist_egg(pdir, destdir)
            
        print 'exporting archive of repository to %s' % tarname
        check_call(['git', 'archive', '-o', tarname, 'HEAD'])
    
        print 'creating bootstrapping installer script go-openmdao.py'
        installer = os.path.join(topdir, 'scripts','mkinstaller.py')
        
        if options.disturl != PRODUCTION_DISTS_URL:
            check_call([sys.executable, installer, '--disturl', options.disturl,
                        '--dest=%s'%destdir])
            ## roll back changes to releaseinfo.py files
            #for project_name, pdir, pkgtype in openmdao_packages:
                #pdir = os.path.join(topdir, pdir, project_name)
                #if 'src' in os.listdir(pdir):
                    #os.chdir(os.path.join(pdir, 'src'))
                #else:
                    #os.chdir(pdir)
                #rollback_releaseinfo_file(project_name, releaseinfo_str)
        else:
            check_call([sys.executable, installer, '--dest=%s'%destdir])
            if options.comment:
                comment = options.comment
            else:
                comment = 'creating release %s' % options.version
            
            # tag the current revision with the release version id
            print "tagging release with '%s'" % options.version
            check_call(['git', 'tag', '-a', options.version, '-m', comment])
    finally:
        if has_checkouts:
            ???
        os.chdir(startdir)
    
if __name__ == '__main__':
    main()
