# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License..

import abc
import collections
import functools

from anvil import colorizer
from anvil import exceptions as excp
from anvil import importer
from anvil import log as logging
from anvil import packager
from anvil import phase
from anvil import shell as sh
from anvil import utils

LOG = logging.getLogger(__name__)


class PhaseFunctors(object):
    def __init__(self, start, run, end):
        self.start = start
        self.run = run
        self.end = end


class Action(object):
    __meta__ = abc.ABCMeta

    def __init__(self, distro, cfg, root_dir, **kargs):
        self.distro = distro
        self.cfg = cfg
        self.keep_old = kargs.get('keep_old', False)
        self.force = kargs.get('force', False)
        self.root_dir = root_dir

    @staticmethod
    def get_lookup_name():
        raise NotImplementedError()

    @staticmethod
    def get_action_name():
        raise NotImplementedError()

    @abc.abstractmethod
    def _run(self, persona, component_order, instances):
        """Run the phases of processing for this action.

        Subclasses are expected to override this method to
        do something useful.
        """

    def _order_components(self, components):
        """Returns the components in the order they should be processed.
        """
        # Duplicate the list to avoid problems if it is updated later.
        return list(components)

    def _get_component_dirs(self, component):
        component_dir = sh.joinpths(self.root_dir, component)
        trace_dir = sh.joinpths(component_dir, 'traces')
        app_dir = sh.joinpths(component_dir, 'app')
        cfg_dir = sh.joinpths(component_dir, 'config')
        return {
            'app_dir': app_dir,
            'cfg_dir': cfg_dir,
            'component_dir': component_dir,
            'root_dir': self.root_dir,
            'trace_dir': trace_dir,
        }

    def _merge_options(self, name, base_opts, component_opts, persona_opts):
        joined_opts = dict()
        joined_opts.update(self._get_component_dirs(name))
        if base_opts:
            joined_opts.update(base_opts)
        if component_opts:
            joined_opts.update(component_opts)
        if persona_opts:
            joined_opts.update(persona_opts)
        return joined_opts

    def _merge_subsystems(self, component_subsys, desired_subsys):
        joined_subsys = {}
        if not component_subsys:
            component_subsys = dict()
        if not desired_subsys:
            return joined_subsys
        for subsys in desired_subsys:
            if subsys in component_subsys:
                joined_subsys[subsys] = component_subsys[subsys]
            else:
                joined_subsys[subsys] = {}
        return joined_subsys

    def _convert_siblings(self, siblings):
        mp = {}
        for (action, cls_name) in siblings.items():
            mp[action] = importer.import_entry_point(cls_name)
        return mp

    def _construct_instances(self, persona):
        """
        Create component objects for each component in the persona.
        """
        persona_subsystems = persona.wanted_subsystems or {}
        persona_opts = persona.component_options or {}
        instances = {}
        base_options = {
            'keep_old': self.keep_old,
        }
        for c in persona.wanted_components:
            ((cls, opts), siblings) = self.distro.extract_component(c, self.get_lookup_name())
            LOG.debug("Constructing component %s (%s)", c, utils.obj_name(cls))
            cls_kvs = {}
            cls_kvs['runner'] = self
            cls_kvs['siblings'] = self._convert_siblings(siblings)
            cls_kvs['subsystems'] = self._merge_subsystems(opts.pop('subsystems', None),
                                                           persona_subsystems.get(c))
            cls_kvs['instances'] = instances
            cls_kvs['name'] = c
            cls_kvs['packager_functor'] = functools.partial(packager.get_packager,
                                                            distro=self.distro)
            # Can't override the above
            component_opts = {}
            for (k, v) in opts.items():
                if k not in cls_kvs:
                    component_opts[k] = v
            cls_kvs['options'] = self._merge_options(c, base_options, 
                                                     component_opts, persona_opts.get(c))
            LOG.debug("Construction of %s params are:", c)
            utils.log_object(cls_kvs, logger=LOG, level=logging.DEBUG)
            instances[c] = cls(**cls_kvs)
        return instances

    def _verify_components(self, component_order, instances):
        LOG.info("Verifying that the components are ready to rock-n-roll.")
        for c in component_order:
            instances[c].verify()

    def _warm_components(self, component_order, instances):
        LOG.info("Warming up component configurations.")
        for c in component_order:
            instances[c].warm_configs()

    def _get_phase_directory(self, action_name=None):
        if not action_name:
            action_name = self.get_action_name()
        return sh.joinpths(self.root_dir, "phases", action_name)

    def _get_phase_filename(self, phase_name, action_name=None):
        dirname = self._get_phase_directory(action_name)
        sh.mkdirslist(dirname)
        return sh.joinpths(dirname, "%s.phases" % (phase_name.lower()))

    def _run_phase(self, functors, component_order, instances, phase_name):
        """
        Run a given 'functor' across all of the components, in order.
        """
        component_results = dict()
        if phase_name:
            phase_recorder = phase.PhaseRecorder(self._get_phase_filename(phase_name))
        else:
            phase_recorder = phase.NullPhaseRecorder()
        # Reset all activations
        for c in component_order:
            instance = instances[c]
            instance.activated = False
        # Run all components which have not been activated
        try:
            for c in component_order:
                instance = instances[c]
                if c in phase_recorder:
                    LOG.debug("Skipping phase named %r for component %r since it already happened.", phase_name, c)
                    instance.activated = True
                    component_results[c] = None
                else:
                    try:
                        result = None
                        with phase_recorder.mark(c):
                            if functors.start:
                                functors.start(instance)
                            if functors.run:
                                result = functors.run(instance)
                            if functors.end:
                                functors.end(instance, result)
                        component_results[instance] = result
                        instance.activated = True
                    except (excp.NoTraceException) as e:
                        if self.force:
                            LOG.debug("Skipping exception: %s" % (e))
                        else:
                            raise
            self._on_completion(phase_name, component_results)
        finally:
            # Reset all activations
            for c in component_order:
                instance = instances[c]
                instance.activated = False
        return component_results

    def _get_opposite_stages(self, phase_name):
        return ('', [])

    def _on_completion(self, phase_name, results):
       (action_name, to_destroy) = self._get_opposite_stages(phase_name)
       for name in to_destroy:
           fn = self._get_phase_filename(name, action_name)
           if sh.isfile(fn):
               sh.unlink(fn)

    def run(self, persona):
        instances = self._construct_instances(persona)
        component_order = self._order_components(persona.wanted_components)
        LOG.info("Processing components for action %s.", colorizer.quote(self.get_action_name()))
        utils.log_iterable(component_order,
                        header="Activating in the following order",
                        logger=LOG)
        self._verify_components(component_order, instances)
        self._warm_components(component_order, instances)
        self._run(persona, component_order, instances)
        return component_order
