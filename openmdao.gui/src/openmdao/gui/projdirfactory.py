
#public symbols
__all__ = ["ProjDirFactory"]

import os
import sys
import threading
import fnmatch
import ast
from inspect import isclass, getmembers
import imp

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from zope.interface import implementedBy

import openmdao.main.api
import openmdao.main.datatypes.api
from openmdao.main.interfaces import IContainer, IComponent, IAssembly, IDriver, \
                                     IDOEgenerator, ISurrogate, ICaseFilter, ICaseIterator, ICaseRecorder, \
                                     IArchitecture, IDifferentiator

from openmdao.main.factory import Factory
from openmdao.main.factorymanager import get_available_types
from openmdao.util.dep import find_files, plugin_groups, PythonSourceTreeAnalyser
from openmdao.util.fileutil import get_module_path, get_ancestor_dir
from openmdao.util.log import logger
from openmdao.main.publisher import Publisher
from openmdao.gui.util import packagedict

class PyWatcher(FileSystemEventHandler):

    def __init__(self, factory):
        super(PyWatcher, self).__init__()
        self.factory = factory

    def on_modified(self, event):
        added_set = set()
        changed_set = set()
        deleted_set = set()
        if not event.is_directory and fnmatch.fnmatch(event.src_path, '*.py'):
            compiled = event.src_path+'c'
            if os.path.exists(compiled):
                os.remove(compiled)
            self.factory.on_modified(event.src_path, added_set, changed_set, deleted_set)
            self.factory.publish_updates(added_set, changed_set, deleted_set)

    on_created = on_modified
    
    def on_moved(self, event):
        added_set = set()
        changed_set = set()
        deleted_set = set()
        
        publish = False
        if event._src_path and (event.is_directory or fnmatch.fnmatch(event._src_path, '*.py')):
            if not event.is_directory:
                compiled = event._src_path+'c'
                if os.path.exists(compiled):
                    os.remove(compiled)
            self.factory.on_deleted(event._src_path, deleted_set)
            publish = True
        
        if fnmatch.fnmatch(event._dest_path, '*.py'):
            self.factory.on_modified(event._dest_path, added_set, changed_set, deleted_set)
            publish = True
            
        if publish:
            self.factory.publish_updates(added_set, changed_set, deleted_set)

    def on_deleted(self, event):
        added_set = set()
        changed_set = set()
        deleted_set = set()
        if event.is_directory or fnmatch.fnmatch(event.src_path, '*.py'):
            compiled = event.src_path+'c'
            if os.path.exists(compiled):
                os.remove(compiled)
            self.factory.on_deleted(event.src_path, deleted_set)
            self.factory.publish_updates(added_set, changed_set, deleted_set)
            
plugin_ifaces = set([
    'IContainer', 
    'IComponent', 
    'IAssembly', 
    'IDriver', 
    'IDOEgenerator', 
    'ISurrogate', 
    'ICaseFilter', 
    'ICaseIterator', 
    'ICaseRecorder',
    'IArchitecture', 
    'IDifferentiator',
])

def _find_module_attr(modpath):
    """Return an attribute from a module based on the given modpath.
    Import the module if necessary.
    """
    parts = modpath.split('.')
    if len(parts) <= 1:
        return None
    
    mname = '.'.join(parts[:-1])
    mod = sys.modules.get(mname)
    if mod is None:
        try:
            __import__(mname)
            mod = sys.modules.get(mname)
        except ImportError:
            pass
    if mod:
        return getattr(mod, parts[-1])
    
    # try one more level down in case attr is nested
    obj = _find_module_attr(mname)
    if obj:
        obj = getattr(obj, parts[-1], None)
    return obj
    
class _ClassVisitor(ast.NodeVisitor):
    def __init__(self, fname):
        ast.NodeVisitor.__init__(self)
        self.classes = []
        
        # in order to get this to work with the 'ast' lib, I have
        # to read using universal newlines and append a newline
        # to the string I read for some files.  The 'compiler' lib
        # didn't have this problem. :(
        with open(fname, 'Ur') as f:
            contents = f.read()
            if len(contents)>0 and contents[-1] != '\n':
                contents += '\n'
        self.visit(ast.parse(contents, fname))
        
    def visit_ClassDef(self, node):
        self.classes.append(node.name)

class _FileInfo(object):
    def __init__(self, fpath):
        self.fpath = fpath
        self.modpath = get_module_path(fpath)
        self.modtime = os.path.getmtime(fpath)
        if self.modpath in sys.modules:
            mod = sys.modules[self.modpath]
            print '   reloading %s' % self.modpath
            self._reload()
        else:
            print '   importing %s' % self.modpath
            __import__(self.modpath)
        module = sys.modules[self.modpath]
        self.version = getattr(module, '__version__', None)
        self._update_class_info()
    
    def _update_class_info(self):
        self.classes = {}
        cset = set(['.'.join([self.modpath,cname]) for cname in _ClassVisitor(self.fpath).classes])
        print "cset = %s" % list(cset)
        module = sys.modules[self.modpath]
        for key,val in getmembers(module, isclass):
            fullname = '.'.join([self.modpath, key])
            if fullname in cset:
                self.classes[fullname] = {
                    'ctor': val,
                    'ifaces': [klass.__name__ for klass in implementedBy(val)],
                    'version': self.version,
                }
        
    def _reload(self):
        cmpfname = os.path.splitext(self.fpath)[0]+'.pyc'
        # unless we remove the .pyc file, reload will just use it and won't
        # see any source updates.  :(
        if os.path.isfile(cmpfname):
            os.remove(cmpfname)
        reload(sys.modules[self.modpath])
        
    def update(self, added, changed, removed):
        """File has changed on disk, update information and return 
        sets of added, removed and (possibly) changed classes.
        """
        self.modpath = get_module_path(self.fpath)
        startset = set(self.classes.keys())
        cmpfname = os.path.splitext(self.fpath)[0]+'.pyc'
        self._reload()
        self._update_class_info()
        
        keys = set(self.classes.keys())
        added.update(keys - startset)
        changed.update(startset & keys)
        removed.update(startset - keys)


class ProjDirFactory(Factory):
    """A Factory that watches a Project directory and dynamically keeps
    the set of available types up-to-date as project files change.
    """
    def __init__(self, watchdir, use_observer=True, observer=None):
        super(ProjDirFactory, self).__init__()
        self._lock = threading.RLock()
        self.observer = None
        self.watchdir = watchdir
        self.project = None
        self._files = {} # mapping of file pathnames to _FileInfo objects
        self._classes = {} # mapping of class names to _FileInfo objects
        try:
            added_set = set()
            changed_set = set()
            deleted_set = set()
            for pyfile in find_files(self.watchdir, "*.py"):
                self.on_modified(pyfile, added_set, changed_set, deleted_set)
            
            if use_observer:
                self._start_observer(observer)
                self.publish_updates(added_set, changed_set, deleted_set)
            else:
                self.observer = None  # sometimes for debugging/testing it's easier to turn observer off
        except Exception as err:
            logger.error(str(err))

    def _start_observer(self, observer):
        if observer is None:
            self.observer = Observer()
            self._ownsobserver = True
        else:
            self.observer = observer
            self._ownsobserver = False
        self.observer.schedule(PyWatcher(self), path=self.watchdir, recursive=True)
        if self._ownsobserver:
            print "starting observer"
            self.observer.daemon = True
            self.observer.start()
        
    def create(self, typ, version=None, server=None, 
               res_desc=None, **ctor_args):
        """Create and return an instance of the specified type, or None if
        this Factory can't satisfy the request.
        """
        if server is None and res_desc is None:
            try:
                klass = self._classes[typ].classes[typ]['ctor']
            except KeyError:
                return None
            
            return klass(**ctor_args)
    
    def get_available_types(self, groups=None):
        """Return a list of available types."""
        with self._lock:
            typset = set(self._classes.keys())
            types = []
        
            if groups is None:
                ifaces = set([v[0] for v in plugin_groups.values()])
            else:
                ifaces = set([v[0] for k,v in plugin_groups.items() if k in groups])
        
            for typ in typset:
                finfo = self._classes[typ]
                meta = finfo.classes[typ]
                print 'for %s, ifaces = %s' % (typ, meta['ifaces'])
                if ifaces.intersection(meta['ifaces']):
                    meta = { 
                        'ifaces': meta['ifaces'],
                        'version': meta['version'],
                        '_context': 'In Project',
                    }
                    types.append((typ, meta))
            return types

    def on_modified(self, fpath, added_set, changed_set, deleted_set):
        if os.path.isdir(fpath):
            return None
        
        with self._lock:
            finfo = self._files.get(fpath)
            if finfo is None:
                print 'new file %s' % fpath
                fileinfo = _FileInfo(fpath)
                self._files[fpath] = fileinfo
                added_set.update(fileinfo.classes.keys())
                for cname in fileinfo.classes.keys():
                    self._classes[cname] = fileinfo
                print "finished processing of %s" % fpath
            else: # updating a file that's already been imported
                print "updating existing file %s" % fpath
                finfo.update(added_set, changed_set, deleted_set)
                for cname in added_set:
                    self._classes[cname] = finfo
                for cname in deleted_set:
                    del self._classes[cname]
                print "finished processing of %s" % fpath
                
    def on_deleted(self, fpath, deleted_set):
        with self._lock:
            if os.path.isdir(fpath):
                for pyfile in find_files(self.watchdir, "*.py"):
                    self.on_deleted(pyfile, deleted_set)
            else:
                finfo = self._files[fpath]
                deleted_set.update(finfo.classes.keys())
                for cname in finfo.classes:
                    del self._classes[cname]
                del self._files[fpath]
            
    def publish_updates(self, added_set, changed_set, deleted_set):
        publisher = Publisher.get_instance()
        if publisher:
            types = get_available_types()
            publisher.publish('types', 
                              [
                                  packagedict(types),
                                  list(added_set),
                                  list(changed_set),
                                  list(deleted_set),
                              ])
        else:
            logger.error("no Publisher found")

    def cleanup(self):
        """If this factory is removed from the FactoryManager during execution, this function
        will stop the watchdog observer thread.
        """
        if self.observer and self._ownsobserver:
            self.observer.unschedule_all()
            self.observer.stop()
            self.observer.join()

if __name__ == '__main__':
    import time
    event_handler = PyWatcher()
    observer = Observer()
    observer.schedule(event_handler, path='.', recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(.1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
