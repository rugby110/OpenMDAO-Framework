"""
Test distributed simulation.
"""

import cPickle
import glob
import hashlib
import logging
from math import pi
from multiprocessing import AuthenticationError, current_process
from multiprocessing.managers import RemoteError
import os
import shutil
import socket
import sys
import traceback
import unittest
import nose

from Crypto.Random import get_random_bytes

from enthought.traits.api import TraitError

from openmdao.main.api import Assembly, Case, Component, Container, Driver, \
                              set_as_top
from openmdao.main.container import get_closest_proxy
from openmdao.main.hasobjective import HasObjectives
from openmdao.main.hasparameters import HasParameters
from openmdao.main.interfaces import IComponent
from openmdao.main.mp_support import has_interface, is_instance
from openmdao.main.mp_util import read_server_config
from openmdao.main.objserverfactory import connect, start_server, RemoteFile
from openmdao.main.rbac import Credentials, get_credentials, set_credentials, \
                               AccessController, RoleError, rbac

from openmdao.lib.datatypes.api import Float, Int
from openmdao.lib.caserecorders.listcaserecorder import ListCaseRecorder

from openmdao.test.execcomp import ExecComp

from openmdao.util.decorators import add_delegate
from openmdao.util.publickey import generate_key_pair
from openmdao.util.testutil import assert_raises, assert_rel_error


# Used for naming classes we want to create instances of.
_MODULE = 'openmdao.main.test.test_distsim'

# Used for naming server directories.
_SERVER_ID = 0


class Box(ExecComp):
    """ Simple component for testing. """

    pid = Int(iotype='out')

    def __init__(self):
        super(Box, self).__init__([
            'surface_area = (width*(height+depth) + depth*height)*2',
            'volume = width*height*depth'])
        self.pid = os.getpid()
        # For get_closest_proxy().
        sub = self.add('subcontainer', Container())
        sub.add_trait('subvar', Int())

    def execute(self):
        print 'Box.execute(), %f %f %f on %d' \
              % (self.width, self.height, self.depth, self.pid)
        super(Box, self).execute()

    def no_rbac(self):
        pass

    @rbac('owner', proxy_types=[RemoteFile])
    def open_in_parent(self, path, mode):
        try:
            return self.parent.open(path, mode)
        except Exception as exc:
            self._logger.debug('open_in_parent() caught %s:', exc)
            self._logger.debug(traceback.format_exc())

    @rbac('owner')
    def cause_parent_error1(self):
        return self.parent.no_such_variable

    @rbac('owner')
    def cause_parent_error2(self):
        return self.parent.get_trait('no-such-trait')

    @rbac('owner')
    def cause_parent_error3(self):
        return self.parent.xyzzy()


class HollowSphere(Component):
    """ Simple component for testing. """

    radius = Float(1.0, low=0., exclude_low=True, iotype='in', units='cm')
    thickness = Float(0.05, iotype='in', units='cm')

    inner_volume = Float(iotype='out', units='cm**3')
    volume = Float(iotype='out', units='cm**3')
    solid_volume = Float(iotype='out', units='cm**3')
    surface_area = Float(iotype='out', units='cm**2')
    pid = Int(iotype='out')

    def __init__(self, doc=None, directory=''):
        super(HollowSphere, self).__init__(doc, directory) 
        self.pid = os.getpid()

    def execute(self):
        self.surface_area = 4.0*pi*self.radius*self.radius
        self.inner_volume = 4.0/3.0*pi*self.radius**3
        self.volume = 4.0/3.0*pi*(self.radius+self.thickness)**3
        self.solid_volume = self.volume-self.inner_volume


@add_delegate(HasParameters)
@add_delegate(HasObjectives)
class BoxDriver(Driver):
    """ Just drives :class:`Box` inputs and records results. """

    def __init__(self):
        super(BoxDriver, self).__init__()
        self.recorder = ListCaseRecorder()

    def execute(self):
        """ Runs with various box parameter values. """
        for width in range(1, 2):
            for height in range(1, 3):
                for depth in range(1, 4):
                    self._logger.debug('w,h,d %s, %s, %s', width, height, depth)
                    self.set_parameters((width, height, depth))
                    self.workflow.run()
                    volume, area = self.eval_objectives()
                    self._logger.debug('    v,a %s, %s', volume, area)

                    case = Case()
                    case.inputs = [('width', None, width),
                                   ('height', None, height),
                                   ('depth', None, depth)]
                    case.outputs = [('volume', None, volume),
                                    ('area', None, area),
                                    ('pid', None, self.parent.box.pid)]
                                   # Just to show access to remote from driver.
                    self.recorder.record(case)


class BoxSource(ExecComp):
    """ Just a pass-through for :class:`BoxDriver` input values. """

    def __init__(self):
        super(BoxSource, self).__init__(['width_out  = width_in',
                                         'height_out = height_in',
                                         'depth_out  = depth_in'])
        # For get_closest_proxy().
        sub = self.add('subcontainer', Container())
        sub.add_trait('subvar', Int())

class BoxSink(ExecComp):
    """ Just a pass-through for :class:`BoxDriver` result values. """

    def __init__(self):
        super(BoxSink, self).__init__(['volume_out = volume_in',
                                       'area_out   = area_in'])


class Model(Assembly):
    """ Drive a remote :class:`Box` via connections to local components. """

    def __init__(self, box):
        super(Model, self).__init__()
        self.add('driver', BoxDriver())
        self.driver.workflow.add(self.add('source', BoxSource()))
        self.driver.workflow.add(self.add('box', box))
        self.driver.workflow.add(self.add('sink', BoxSink()))

        self.driver.add_parameter('source.width_in',  low=1e-99, high=1e99)
        self.driver.add_parameter('source.height_in', low=1e-99, high=1e99)
        self.driver.add_parameter('source.depth_in',  low=1e-99, high=1e99)

        self.connect('source.width_out',  'box.width')
        self.connect('source.height_out', 'box.height')
        self.connect('source.depth_out',  'box.depth')

        self.connect('box.volume',       'sink.volume_in')
        self.connect('box.surface_area', 'sink.area_in')

        self.driver.add_objective('sink.volume_out')
        self.driver.add_objective('sink.area_out')

    @rbac('owner', proxy_types=[RemoteFile])
    def open(self, path, mode):
        """ Return opened file. """
        return RemoteFile(open(path, mode))

    @rbac('xyzzy')
    def xyzzy(self):
        """ No access by 'owner', etc. """
        return None


class Protector(AccessController):
    """ Special :class:`AccessController` to protect secrets. """

    def check_access(self, role, methodname, obj, attr):
        if not role:
            raise RoleError('No access by null role')
        if role == 'owner':
            return
        if methodname != '__delattr__' and self.user_attribute(obj, attr):
            return
        raise RoleError("No %s access to '%s' by role '%s'"
                        % (methodname, attr, role))

    @staticmethod
    def user_attribute(obj, attr):
        if attr in obj.list_inputs() or \
           attr in obj.list_outputs() or \
           attr in ('parent', 'name'):
            return True
        return False


class ProtectedBox(Box):
    """ Box which can be used but the innards are hidden. """

    secret = Int()

    def __init__(self):
        super(ProtectedBox, self).__init__()
        # Protector will use current credentials as 'owner'.
        self.protector = Protector()

    @rbac('owner')
    def proprietary_method(self):
        pass

    def get_access_controller(self):
        return self.protector

    @rbac(('owner', 'user'))
    def get(self, path, index=None):
        if self.protector.user_attribute(self, path):
            return super(ProtectedBox, self).get(path, index)
        raise RoleError("No get access to '%s' by role '%s'" % (attr, role))

    @rbac(('owner', 'user'))
    def get_dyn_trait(self, name, iotype=None):
        if self.protector.user_attribute(self, name):
            return super(ProtectedBox, self).get_dyn_trait(name, iotype)
        raise RoleError("No get access to '%s' by role '%s'" % (attr, role))

    @rbac(('owner', 'user'))
    def get_wrapped_attr(self, name):
        if self.protector.user_attribute(self, name):
            return super(ProtectedBox, self).get_wrapped_attr(name)
        raise RoleError("No get_wrapped_attr access to '%s' by role '%s'"
                        % (attr, role))

    @rbac(('owner', 'user'))
    def set(self, path, value, index=None, srcname=None, force=False):
        if self.protector.user_attribute(self, path):
            return super(ProtectedBox, self).set(path, value, index, srcname, force)
        raise RoleError("No set access to '%s' by role '%s'"
                        % (attr, role))


class TestCase(unittest.TestCase):
    """ Test distributed simulation. """

    def run(self, result=None):
        """
        Record the :class:`TestResult` used so we can conditionally cleanup
        directories in :meth:`tearDown`.
        """
        self.test_result = result or unittest.TestResult()
        return super(TestCase, self).run(self.test_result)

    def setUp(self):
        """ Start server process. """
        global _SERVER_ID
        _SERVER_ID += 1

        self.n_errors = len(self.test_result.errors)
        self.n_failures = len(self.test_result.failures)

        # Ensure we control directory cleanup.
        self.keepdirs = os.environ.get('OPENMDAO_KEEPDIRS', '0')
        os.environ['OPENMDAO_KEEPDIRS'] = '1'

        # Start each server process in a unique directory.
        server_dir = 'Factory_%d' % _SERVER_ID
        if os.path.exists(server_dir):
            shutil.rmtree(server_dir)
        os.mkdir(server_dir)
        os.chdir(server_dir)
        self.server_dirs = [server_dir]
        self.server = None
        try:
            logging.debug('')
            logging.debug('tester pid: %s', os.getpid())
            logging.debug('starting server...')
            # Exercise both AF_INET and AF_UNIX/AF_PIPE.
            port = -1 if _SERVER_ID & 1 else 0
            self.server = start_server(port=port)
            self.address, self.port, self.key = read_server_config('server.cfg')
            logging.debug('server pid: %s', self.server.pid)
            logging.debug('server address: %s', self.address)
            logging.debug('server port: %s', self.port)
            logging.debug('server key: %s', self.key)
        finally:
            os.chdir('..')

        self.factory = connect(self.address, self.port, pubkey=self.key)
        logging.debug('factory: %r', self.factory)

    def tearDown(self):
        """ Shut down server process. """
        try:
            if self.factory is not None:
                self.factory.cleanup()
            if self.server is not None:
                logging.debug('terminating server pid %s', self.server.pid)
                self.server.terminate(timeout=10)
                self.server = None

            # Cleanup only if there weren't any new errors or failures.
            if len(self.test_result.errors) == self.n_errors and \
               len(self.test_result.failures) == self.n_failures:
                for server_dir in self.server_dirs:
                    shutil.rmtree(server_dir)
        finally:
            os.environ['OPENMDAO_KEEPDIRS'] = self.keepdirs

    def test_1_client(self):
        logging.debug('')
        logging.debug('test_client')

        # List available types.
        types = self.factory.get_available_types()
        logging.debug('Available types:')
        for typname, version in types:
            logging.debug('   %s %s', typname, version)

        # First a HollowSphere, accessed via get()/set().
        obj = self.factory.create(_MODULE+'.HollowSphere')
        sphere_pid = obj.get('pid')
        self.assertNotEqual(sphere_pid, os.getpid())

        radius = obj.get('radius')
        self.assertEqual(radius, 1.)
        radius += 1
        obj.set('radius', radius)
        new_radius = obj.get('radius')
        self.assertEqual(new_radius, 2.)
        self.assertEqual(obj.get('inner_volume'), 0.)
        self.assertEqual(obj.get('volume'), 0.)
        self.assertEqual(obj.get('solid_volume'), 0.)
        self.assertEqual(obj.get('surface_area'), 0.)
        obj.run()
        assert_rel_error(self, obj.get('inner_volume'), 33.510321638, 0.000001)
        assert_rel_error(self, obj.get('volume'),       36.086951213, 0.000001)
        assert_rel_error(self, obj.get('solid_volume'), 2.5766295747, 0.000001)
        assert_rel_error(self, obj.get('surface_area'), 50.265482457, 0.000001)

        msg = ": Trait 'radius' must be a float in the range (0.0, "
        assert_raises(self, "obj.set('radius', -1)", globals(), locals(),
                      TraitError, msg)

        # Now a Box, accessed via attribute methods.
        obj = self.factory.create(_MODULE+'.Box')
        box_pid = obj.get('pid')
        self.assertNotEqual(box_pid, os.getpid())
        self.assertNotEqual(box_pid, sphere_pid)

        obj.width  += 2
        obj.height += 2
        obj.depth  += 2
        self.assertEqual(obj.width, 2.)
        self.assertEqual(obj.height, 2.)
        self.assertEqual(obj.depth, 2.)
        self.assertEqual(obj.volume, 0.)
        self.assertEqual(obj.surface_area, 0.)
        obj.run()
        self.assertEqual(obj.volume, 8.0)
        self.assertEqual(obj.surface_area, 24.0)

        try:
            obj.no_rbac()
        except RemoteError as exc:
            msg = "AttributeError: method 'no_rbac' of"
            logging.debug('msg: %s', msg)
            logging.debug('exc: %s', exc)
            self.assertTrue(msg in str(exc))
        else:
            self.fail('Expected RemoteError')

    def test_2_model(self):
        logging.debug('')
        logging.debug('test_model')

        # Create model and run it.
        box = self.factory.create(_MODULE+'.Box')
        model = set_as_top(Model(box))
        model.run()

        # Check results.
        for width in range(1, 2):
            for height in range(1, 3):
                for depth in range(1, 4):
                    case = model.driver.recorder.cases.pop(0)
                    self.assertEqual(case.outputs[0][2], width*height*depth)

        self.assertTrue(is_instance(model.box.parent, Assembly))
        self.assertTrue(has_interface(model.box.parent, IComponent))

        # Upcall to use parent to resolve sibling.
        # At one time this caused proxy problems.
        source = model.box.parent.source
        self.assertEqual(source.width_in, 1.)

        # Proxy resolution.
        obj, path = get_closest_proxy(model, 'box.subcontainer.subvar')
        self.assertEqual(obj, model.box)
        self.assertEqual(path, 'subcontainer.subvar')

        obj, path = get_closest_proxy(model, 'source.subcontainer.subvar')
        self.assertEqual(obj, model.source.subcontainer)
        self.assertEqual(path, 'subvar')

        obj, path = get_closest_proxy(model.source.subcontainer, 'subvar')
        self.assertEqual(obj, model.source.subcontainer)
        self.assertEqual(path, 'subvar')

        # Observable proxied type.
        tmp = model.box.open_in_parent('tmp', 'w')
        tmp.close()
        os.remove('tmp')

        # Cause server-side errors we can see.

        try:
            box.cause_parent_error1()
        except RemoteError as exc:
            msg = "AttributeError: attribute 'no_such_variable' of"
            logging.debug('msg: %s', msg)
            logging.debug('exc: %s', exc)
            self.assertTrue(msg in str(exc))
        else:
            self.fail('Expected RemoteError')

        try:
            box.cause_parent_error2()
        except RemoteError as exc:
            msg = "AttributeError: method 'get_trait' of"
            logging.debug('msg: %s', msg)
            logging.debug('exc: %s', exc)
            self.assertTrue(msg in str(exc))
        else:
            self.fail('Expected RemoteError')

        try:
            box.cause_parent_error3()
        except RemoteError as exc:
            msg = "RoleError: xyzzy(): No access for role 'owner'"
            logging.debug('msg: %s', msg)
            logging.debug('exc: %s', exc)
            self.assertTrue(msg in str(exc))
        else:
            self.fail('Expected RemoteError')

    def test_3_access(self):
        logging.debug('')
        logging.debug('test_access')

        # This 'spook' creation is only for testing.
        # Normally the protector would run with regular credentials
        # in effect at the proprietary site.
        user = 'spooky@spooks-r-us.com'
        key_pair = generate_key_pair(user)
        data = '\n'.join([user, '0', key_pair.publickey().exportKey()])
        hash = hashlib.sha256(data).digest()
        signature = key_pair.sign(hash, get_random_bytes)
        spook = Credentials(data, signature)

        # Create model and run it.
        saved = get_credentials()
        set_credentials(spook)
        box = self.factory.create(_MODULE+'.ProtectedBox')
        set_credentials(saved)

        model = set_as_top(Model(box))
        model.run()

        # Check results.
        for width in range(1, 2):
            for height in range(1, 3):
                for depth in range(1, 4):
                    case = model.driver.recorder.cases.pop(0)
                    self.assertEqual(case.outputs[0][2], width*height*depth)

        # Check access protections.
        try:
            i = model.box.secret
        except RemoteError as exc:
            msg = "RoleError: No __getattribute__ access to 'secret' by role 'user'"
            logging.debug('msg: %s', msg)
            logging.debug('exc: %s', exc)
            self.assertTrue(msg in str(exc))
        else:
            self.fail('Expected RemoteError')

        try:
            model.box.proprietary_method()
        except RemoteError as exc:
            msg = "RoleError: proprietary_method(): No access for role 'user'"
            logging.debug('msg: %s', msg)
            logging.debug('exc: %s', exc)
            self.assertTrue(msg in str(exc))
        else:
            self.fail('Expected RemoteError')

        saved = get_credentials()
        set_credentials(spook)
        try:
            i = model.box.secret
            model.box.proprietary_method()
        finally:
            # Reset credentials to allow factory shutdown.
            set_credentials(saved)

    def test_4_authkey(self):
        logging.debug('')
        logging.debug('test_authkey')

        # Start server in non-public-key mode.
        # Connections must have matching authkey,
        # but data is sent in the clear!?
        # This is standard multiprocessing behaviour.
        authkey = 'password'
        server_dir = 'Factory_authkey'
        if os.path.exists(server_dir):
            shutil.rmtree(server_dir)
        os.mkdir(server_dir)
        os.chdir(server_dir)
        self.server_dirs.append(server_dir)
        try:
            logging.debug('starting server (authkey %s)...', authkey)
            server = start_server(authkey=authkey, timeout=30)
            address, port, key = read_server_config('server.cfg')
            logging.debug('server address: %s', address)
            logging.debug('server port: %s', port)
            logging.debug('server key: %s', key)
        finally:
            os.chdir('..')

        factory = None
        try:
            assert_raises(self, 'connect(address, port, pubkey=key)',
                          globals(), locals(), AuthenticationError,
                          'digest sent was rejected')

            factory = connect(address, port, authkey=authkey)
            logging.debug('factory: %r', factory)

            # Create model and run it.
            box = factory.create(_MODULE+'.Box')
            model = set_as_top(Model(box))
            model.run()

            # Check results.
            for width in range(1, 2):
                for height in range(1, 3):
                    for depth in range(1, 4):
                        case = model.driver.recorder.cases.pop(0)
                        self.assertEqual(case.outputs[0][2], width*height*depth)
        finally:
            if factory is not None:
                factory.cleanup()
            logging.debug('terminating server (authkey %s) pid %s',
                          authkey, server.pid)
            server.terminate(timeout=10)
            server = None

    def test_5_misc(self):
        logging.debug('')
        logging.debug('test_misc')

        # Try using a server after being released, server never used before.
        server = self.factory.create('')
        self.factory.release(server)
        assert_raises(self, "server.echo('hello')", globals(), locals(),
                      RuntimeError, "Can't connect to server at")

        # Try using a server after being released, server has been used before.
        server = self.factory.create('')
        reply = server.echo('hello')
        self.factory.release(server)
        assert_raises(self, "server.echo('hello')", globals(), locals(),
                      RuntimeError, "Can't send to server at")

        # Try releasing a server twice. Depending on timing, this could
        # result in a ValueError trying to identify the server to release or
        # a RemoteError where the request can't be unpacked. The timing seems
        # to be sensitive to AF_INET/AF_UNIX connection type.
        server = self.factory.create('')
        self.factory.release(server)
        msg1 = "can't identify server "
        msg2 = "RuntimeError: Can't decrypt/unpack request." \
               " This could be the result of referring to a dead server."
        try:
            self.factory.release(server)
        except ValueError as exc:
            self.assertEqual(str(exc)[:len(msg1)], msg1)
        except RemoteError as exc:
            self.assertTrue(msg2 in str(exc))
        else:
            self.fail('Expected ValueError or RemoteError')

        # Check false return of has_interface().
        self.assertFalse(has_interface(self.factory, HasObjectives))

        # Try to connect to wrong port (assuming junk_port isn't being used!)
        address = socket.gethostname()
        junk_port = 12345
        assert_raises(self, 'connect(address, junk_port, pubkey=self.key)',
                      globals(), locals(), RuntimeError, "can't connect to ")

        # Unpickleable argument.
        code = compile('3 + 4', '<string>', 'eval')
        assert_raises(self, 'self.factory.echo(code)', globals(), locals(),
                      cPickle.PicklingError, "Can't pickle <type 'code'>")


if __name__ == '__main__':
    sys.argv.append('--cover-package=openmdao.main')
    sys.argv.append('--cover-erase')
    nose.runmodule()

