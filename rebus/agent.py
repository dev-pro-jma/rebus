from rebus.tools.registry import Registry
from rebus.tools.config import get_output_altering_options
from rebus.bus import DEFAULT_DOMAIN
from collections import defaultdict
import logging
import time
import json
import cPickle
log = logging.getLogger("rebus.agent")


class AgentRegistry(Registry):
    pass


class AgentLogger(logging.LoggerAdapter):
    def process(self, msg, kargs):
        return "[%s] %s" % (self.extra["agent_id"], msg), kargs


class Agent(object):
    _name_ = "Agent"
    _desc_ = "N/A"
    _process_slots_ = None

    #: Supported operation modes. Actual operation mode is chosen at launch;
    #: may be changed on master bus' order.
    #: default mode as 1st element.
    _operationmodes_ = ('automatic', 'interactive', 'idle')
    #: tuple of names of options that influence output values. If not
    #: overridden, every option except 'operationmode' will be considered as
    #: influencing the output.
    _output_altering_options_ = None

    @staticmethod
    def register(f):
        return AgentRegistry.register_ref(f, key="_name_")

    # Use this decorator on an agent class to authorize concurrent runs if possible
    @staticmethod
    def parallelize(max_thread=0):
        def deco(agt):
            agt._parallelize_ = {"max_thread":max_thread}
            return agt
        return deco

    def __init__(self, bus, options, name=None, domain='default'):
        self.name = name if name else self._name_
        self.domain = domain
        self.bus = bus
        #: {key: value} containing relevant parameters that may influence the
        #: agent's outputs
        self.config = vars(options)
        # Chosen operation mode
        if len(self._operationmodes_) == 1:
            self.config['operationmode'] = self._operationmodes_[0]
        if self.config['operationmode'] == 'idle':
            self.for_idle = []
        if self._output_altering_options_ is None:
            self.config['output_altering_options'] = self.config.keys()
            self.config['output_altering_options'].remove('operationmode')
        else:
            self.config['output_altering_options'] = \
                self._output_altering_options_
        self.id = self.bus.join(self, domain)
        self.log = AgentLogger(log, dict(agent_id=self.id))
        self.log.info('Agent {0.name} registered on bus {1._name_} '
                      'with id {0.id}'.format(self, self.bus))
        self.process_slots = defaultdict(dict)
        # Updated when starting processing
        self.processing_start_time = 0
        self.init_agent()
        self.restore_internal_state()

    def push(self, descriptor):
        if descriptor.processing_time == -1:
            descriptor.processing_time = time.time() - self.processing_start_time
        result = self.bus.push(self.id, descriptor)
        self.log.debug("pushed {0}, already present: {1}".format(descriptor,
                                                                 not result))
        return result

    def get(self, desc_domain, selector):
        return self.bus.get(self.id, desc_domain, selector)

    def find(self, domain, selector_regex, limit):
        return self.bus.find(self.id, domain, selector_regex, limit)

    def list_uuids(self, desc_domain):
        return self.bus.list_uuids(self.id, desc_domain)

    def list_agents(self):
        return self.bus.list_agents(self.id)

    def processed_stats(self, desc_domain):
        return self.bus.processed_stats(self.id, desc_domain)

    def lock(self, lockid, desc_domain, selector):
        return self.bus.lock(self.id, lockid, desc_domain, selector)

    def slots_are_processable(self, slots):
        """
        Test if a set of slots is ready to be processed. By default, this methods checks
        that the slot set is complete, but an agent can overload it to have incomplete 
        sets to processed anyway, for instance if one slot is not mandatory.
        """
        return len(slots) == len(self._process_slots_)

    def on_idle(self):
        """
        on_idle is called by the bus when all descriptors have been processed
        or marked as processable, allowing agents in the 'idle' operation mode
        to only start processing when all the other agents have finished.
        it should return True if it processed descriptors,
        False if not
        """
        if self.config['operationmode'] != 'idle':
            return False

        self.log.info("START on_idle bulk processing %d descriptors", 
                                                    len(self.for_idle))
        self.processing_start_time = time.time()
        for params in self.for_idle:
            self.call_process(*params)
        self.for_idle = []
        self.log.info("END  on_idle bulk processing  |%f|",
                                    time.time()-self.processing_start_time)

        return True

    def on_new_descriptor(self, sender_id, desc_domain, uuid, selector, 
                          request_id=0):
        """
        request_id is 0 for automatic processing.
        A unique id should be used for each interactive (user-requested)
        processing.
        """
        self.log.debug("Received from %s descriptor [%s:%s] for UUID %s", 
                       sender_id, desc_domain, selector, uuid)
        if self.domain != DEFAULT_DOMAIN and desc_domain != self.domain:
            # this agent only processes descriptors whose domain is self.domain
            self.bus.mark_processed(self.id, desc_domain, selector)
            return
        fres = self.selector_filter(selector)
        if not fres:
            # not interested in this
            self.bus.mark_processed(self.id, desc_domain, selector)
            return
        slots = {}
        if self._process_slots_:
            assert fres in self._process_slots_
            slots = self.process_slots[uuid]
            slots[fres] = selector
            self.log.info("Filling slot %s for %s. Filling level %i/%i." % 
                          (fres, uuid, len(slots), len(self._process_slots_)))
            if not self.slots_are_processable(slots):
                self.bus.mark_processable(self.id, desc_domain, selector)
                return

        if self.config['operationmode'] == 'interactive' and not request_id:
            self.bus.mark_processable(self.id, desc_domain, selector)
            return
        elif self.config['operationmode'] == 'idle':
            self.bus.mark_processable(self.id, desc_domain, selector)
            self.for_idle.append((sender_id, desc_domain, selector, slots))
            return


        self.bus.agent_process(sender_id, desc_domain, selector, slots, request_id)

    def call_process(self, sender_id, desc_domain, selector, slots, request_id=0):

        lockid = self.name + get_output_altering_options(self.config_txt) + \
            str(request_id)
        # In case of slots, lock on all the selectors at once, so that if one 
        # optional selector is missing at the time of the lock, another lock
        # will be taken when this selector is received and processing the complete 
        # set of slots will not be blocked.
        if self._process_slots_:
            locksel = "!".join(slots.get(s,"?") for s in self._process_slots_)
        else:
            locksel = selector
        
        if not self.lock(lockid, desc_domain, locksel):
            # processing has already been started by another instance of
            # the same agent having the same configuration
            return
        desc = self.get(desc_domain, selector)
        if desc is None:
            log.warning("Descriptor %s:%s sent by %s does not exist "
                        "(user request: %s)", desc_domain, selector, sender_id,
                        request_id)
            return

        additional_desc = { k: self.get(desc_domain,s) if s != selector else desc
                            for k,s in slots.iteritems() }
        # TODO detect infinite loops ?
        # if self.name in desc.agents:
        #     return  # already processed
        if self.descriptor_filter(desc, **additional_desc):
            self.log.info("START Processing %r", desc)
            self.processing_start_time = time.time()
            self.process(desc, sender_id, **additional_desc)
            done = time.time()
            self.log.info("END   Processing |%f| %r",
                          done-self.processing_start_time, desc)
        if additional_desc:
            for adesc in additional_desc.itervalues():
                self.bus.mark_processed(self.id, desc_domain, adesc.selector)
        else:
            self.bus.mark_processed(self.id, desc_domain, selector)

    @property
    def config_txt(self):
        return json.dumps(self.config, sort_keys=True)

    def declare_link(self, desc1, desc2, linktype, reason):
        """
        Helper function.
        Requests two new /link/ descriptors, then pushes them.
        :param desc1: Descriptor instance
        :param desc2: Descriptor instance
        :param linktype: word describing the type of the link, that will be
          part of the selector
        :param reason: Text description of the link reason
        """
        link1, link2 = desc1.create_links(desc2, self.name, linktype, reason)
        self.push(link1)
        self.push(link2)

    def get_value(self, descriptor):
        if hasattr(descriptor, 'value'):
            return descriptor.value
        else:
            # TODO request from storage if locally available - implement when
            # agent has a reference to possibly existent local storage
            return self.bus.get_value(self.id, descriptor.domain,
                                      descriptor.selector)
            # possible trade-off: store now-fetched value in descriptor

    def get_processable(self, domain, selector):
        return self.bus.get_processable(self.id, domain, selector)

    def request_processing(self, domain, selector, targets):
        return self.bus.request_processing(self.id, domain, selector, targets)

    def save_internal_state(self):
        """
        Send internal state to storage. Called at agent shutdown, if persistent
        storage is in use.
        """
        state = self.get_internal_state()
        if state or self._process_slots_:
            complete_state = (state,self.process_slots)
            self.log.info("Save internal state %r" % (complete_state,))
            self.bus.store_internal_state(self.id, cPickle.dumps(complete_state))

    def restore_internal_state(self):
        """
        Retrieve internal state from storage.
        """
        state_ps = self.bus.load_internal_state(self.id)
        self.log.info("Restore state: %r" % state_ps)
        if state_ps:
            state,ps = cPickle.loads(state_ps)
            if self._process_slots_:
                self.log.info("Restore process slot state: %r" % ps)
                self.process_slots = ps
            if state:
                self.log.info("Restore internal state: %r" % state)
                self.set_internal_state(state)

    # These are the main methods that any agent might want to overload
    def init_agent(self):
        """
        Called to initialize the agent, after it has joined the bus.

        The internal state will be restored (set_internal_state) afterwards if
        relevant.
        """
        pass

    def selector_filter(self, selector):
        return True

    def descriptor_filter(self, descriptor, **kwargs):
        return True

    def process(self, descriptor, sender_id, **kargs):
        pass

    def run_and_catch_exc(self):
        try:
            self.run()
        except Exception as e:
            self.log.exception(e)

    def run(self):
        """
        Overriden by agents that do not consume descriptors.

        process() and bulk_process() will not be called if run() is overridden
        """
        pass

    def get_internal_state(self):
        """
        Should be overridden by agents that have an internal state, which
        should be persistent stored when an agent is stopped.

        Return a picklable data structure that contains the internal agent
        state (ex. serialized data structures)
        """
        return

    def set_internal_state(self, state):
        """
        Should be overridden by agents that have an internal state, which
        should be persistently stored when an agent is stopped.

        :param state: data structure that contains the internal agent state
        """
        return

    def __repr__(self):
        return self.id

    @classmethod
    def add_agent_arguments(cls, subparser):
        if len(cls._operationmodes_) > 1:
            subparser.add_argument('--mode', default=cls._operationmodes_[0],
                                   dest='operationmode',
                                   choices=cls._operationmodes_)
        cls.add_arguments(subparser)

    @classmethod
    def add_arguments(cls, subparser):
        """
        Overridden by agents that have configuration parameters
        """
        pass



